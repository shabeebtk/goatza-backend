"""
Pull every existing profile back to town level.

    python manage.py downgrade_precise_locations --dry-run   # report only
    python manage.py downgrade_precise_locations

WHY THIS EXISTS

The profile picker searches in `city` mode and the profile serializer refuses
anything whose type is not `city`, so from now on nobody can put a street
address, a building or a named premise on their profile. Neither of those
rules is retroactive. The rows written before them are still there, and a rule
that is true for new users and false for existing ones is not a rule — it is a
release note. Somebody who picked their apartment building last year is
telling every visitor to their profile where they sleep, and they did it
because the picker let them.

WHAT IT DOES TO A ROW

Each UserProfile whose linked Location has ``type="place"`` is re-resolved to
the town containing it, through the same places service the picker uses: the
Location row already carries city / state / country, which is enough to search
for. On success the profile points at a `city` Location and its denormalized
coordinates follow.

When that is not possible — no city on the row, Google has nothing, the answer
comes back as precise as the input — the coordinates and the FK go, and
``location_name`` / ``city`` / ``country_code`` stay as plain text. That is a
deliberate trade: a profile that reads "Kannur" with no point on the map is a
worse search result than one placed exactly, and a far better outcome than
leaving somebody's building on the internet because a lookup failed.

Safe to run twice. A converted profile now points at a `city` Location and a
nulled one points at nothing, so neither is picked up by the second run.
"""

import uuid

from django.core.management.base import BaseCommand
from django.db import transaction

from accounts.models import UserProfile
from places.services.places_service import (
    MIN_QUERY_LENGTH,
    MODE_CITY,
    PlacesServiceError,
    PlacesUnavailable,
    autocomplete,
    details,
)
from services.location.location_service import LocationService
from shared.models import Location

# The two outcomes a profile can have. Every row handled gets exactly one, so
# converted + nulled is the number of profiles the run finished.
CONVERTED = "converted"
NULLED = "nulled"


def city_query(location):
    """
    The search text for ``location``'s parent town.

    Built from the columns the Location row already has rather than from its
    name: the name is the precise thing we are trying to get away from ("14,
    Beach Road"), while ``city``/``state``/``country`` are what Google's own
    address components said the place sits inside.

    Returns ``""`` when there is not enough to search with — a row with no city
    on it cannot be resolved and takes the nulling path instead.
    """
    parts = [
        (location.city or "").strip(),
        (location.state or "").strip(),
        (location.country or "").strip(),
    ]

    # No city means no query. State and country alone would resolve to a state
    # capital or a country centroid, which is not where this person lives — a
    # confident wrong answer is worse here than no answer.
    if not parts[0]:
        return ""

    seen = []

    for part in parts:
        if part and part not in seen:
            seen.append(part)

    query = ", ".join(seen)

    return query if len(query) >= MIN_QUERY_LENGTH else ""


class Command(BaseCommand):
    help = (
        "Re-resolve profile locations that point at a precise place to the "
        "town containing them, or drop their coordinates when that fails"
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would happen. No Google calls, no writes.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]

        counts = {CONVERTED: 0, NULLED: 0}

        # Diagnostics, NOT part of the outcome partition above. A failed lookup
        # still produces an outcome (the row nulls, which is the safe half of
        # the trade); a failed save produces none at all and leaves the row for
        # the next run.
        lookup_failures = 0
        save_failures = 0

        # One resolution per SOURCE location, not per profile: a gym or an
        # apartment block is routinely on several profiles, and each extra
        # profile pointing at it is a Google call we have already paid for.
        # Maps source location id -> resolved city Location, or None for
        # "asked, and there is no city to be had".
        resolved = {}

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"Precise profile locations{' (dry-run)' if dry_run else ''}"
        ))

        profiles = list(
            UserProfile.objects
            .filter(location__type=Location.Type.PLACE)
            .select_related("location")
            .order_by("created_at")
        )

        self.stdout.write(f"  profiles to downgrade: {len(profiles)}")

        stopped_early = None

        for profile in profiles:
            source = profile.location
            query = city_query(source)

            if dry_run:
                # Deliberately no Google call, so this cannot say which of the
                # two outcomes a row would get — only which one it is eligible
                # for. A report that spends money is not a report.
                if query:
                    self.stdout.write(
                        f"    would look up \"{query}\" for "
                        f"{source.name or '(unnamed)'} [{profile.user_id}]"
                    )
                    counts[CONVERTED] += 1
                else:
                    self.stdout.write(
                        f"    would drop coordinates for "
                        f"{source.name or '(unnamed)'} [{profile.user_id}]"
                    )
                    counts[NULLED] += 1
                continue

            if source.id not in resolved:
                try:
                    resolved[source.id] = (
                        self._resolve_city(source, query) if query else None
                    )
                except PlacesUnavailable:
                    # The daily Details cap is spent, Google answered 429, or
                    # there is no key configured. Every remaining lookup would
                    # be refused the same way, and nulling the rest of the list
                    # on the strength of an outage would throw away
                    # coordinates a later run could have kept.
                    stopped_early = (
                        "daily Places cap reached or Google unavailable"
                    )
                    break
                except (PlacesServiceError, ValueError) as e:
                    self.stderr.write(
                        f"    lookup failed for {source.id}: {type(e).__name__}"
                    )
                    # Not fatal, and not a reason to keep the precise row:
                    # fall through to nulling.
                    resolved[source.id] = None
                    lookup_failures += 1

            city = resolved[source.id]

            try:
                outcome = self._apply(profile, city)
            except Exception as e:
                self.stderr.write(
                    f"    save failed for {profile.user_id}: {type(e).__name__}"
                )
                save_failures += 1
                continue

            counts[outcome] += 1

        if stopped_early:
            self.stdout.write(self.style.WARNING(
                f"  stopped early: {stopped_early}"
            ))

        if dry_run:
            # Named differently on purpose: without a Google call these are
            # eligibility, not outcomes. A row counted resolvable here still
            # nulls if Google has nothing for its town.
            summary = (
                f"resolvable={counts[CONVERTED]} "
                f"no_city={counts[NULLED]} "
                f"(dry-run: nothing written)"
            )
        else:
            summary = (
                f"converted={counts[CONVERTED]} "
                f"nulled={counts[NULLED]} "
                f"lookup_failures={lookup_failures} "
                f"save_failures={save_failures}"
            )

        self.stdout.write("")
        self.stdout.write(
            self.style.WARNING(summary)
            if lookup_failures or save_failures or stopped_early
            else self.style.SUCCESS(summary)
        )

    # ── Resolution ───────────────────────────────────────────────────────────

    def _resolve_city(self, source, query):
        """
        ``query`` -> the ``city`` Location containing ``source``, or None.

        Goes through places_service exactly as the picker does, so the answer
        obeys the same ``includedPrimaryTypes`` filter: whatever comes back is
        a locality, a taluk, a panchayat or a postal town, and cannot be an
        address. One session token covers the autocomplete and the details
        call, which is what makes the pair bill as one session.
        """
        session = str(uuid.uuid4())

        found = autocomplete(q=query, session=session, mode=MODE_CITY)
        results = found.get("results") or []

        if not results:
            return None

        prediction = results[0]
        place_id = prediction.get("place_id") or ""

        # The filter should already have made this impossible, but the whole
        # point of the command is that precise ids do not belong on profiles —
        # so if the "town" comes back as the very place we are moving away
        # from, it is not a parent and this row nulls instead.
        if not place_id or place_id == source.external_id:
            return None

        place = details(place_id=place_id, session=session)

        location = LocationService.get_or_create_location({
            "provider": Location.Provider.GOOGLE,
            # The prediction's main text, not a Details field: the name is free
            # on the prediction and costs an SKU upgrade on Details.
            "name": prediction.get("name") or place.get("city") or source.city,
            "type": Location.Type.CITY,
            "city": place.get("city") or "",
            "state": place.get("state") or "",
            "country": place.get("country") or "",
            "country_code": place.get("country_code") or "",
            "latitude": place.get("latitude"),
            "longitude": place.get("longitude"),
            "external_id": place_id,
        })

        # An existing row for that place id wins in get_or_create, and nothing
        # promises the row somebody else wrote for it is a city. Re-pointing a
        # profile at another `place` row would leave the command with work it
        # thinks it has finished, and a second run would do it all again.
        if location is None or location.type != Location.Type.CITY:
            return None

        return location

    # ── Writing ──────────────────────────────────────────────────────────────

    @transaction.atomic
    def _apply(self, profile, city):
        """
        Point ``profile`` at ``city``, or strip it back to text. Returns the
        outcome key.

        ``updated_at`` is deliberately NOT in update_fields. It is an auto_now
        column and the coordinate-refresh job reads it as "this user was
        recently active" (settings.PLACES_ACTIVE_USER_DAYS) — bumping it here
        would tell that job a few thousand dormant profiles just woke up, and
        it would go and spend a Details call on each of them.
        """
        if city is not None:
            denorm = LocationService.build_denormalized(city)

            profile.location = city
            profile.location_name = denorm["location_name"]
            profile.city = denorm["city"]
            profile.country_code = denorm["country_code"]
            profile.latitude = denorm["latitude"]
            profile.longitude = denorm["longitude"]

            profile.save(update_fields=[
                "location",
                "location_name",
                "city",
                "country_code",
                "latitude",
                "longitude",
            ])

            return CONVERTED

        # The text fallback. The label the user chose stays readable and their
        # point on the map goes: `location` is what the next run would find,
        # and the coordinates are what actually placed them.
        profile.location = None
        profile.latitude = None
        profile.longitude = None

        profile.save(update_fields=["location", "latitude", "longitude"])

        return NULLED
