"""
The coordinate lifecycle — "B-lite" (docs/PLACES_MIGRATION.md section 6).

Google's terms let us keep a ``place_id`` forever but not its coordinates, so a
Location's point is a cache with an expiry rather than a stored fact. Two moves
keep that honest:

  * **Refresh** a place somebody still depends on, through its place_id, once
    every ~25 days. One Details call with the ``location`` field mask and no
    session token — the cheapest call the API has.
  * **Expire** a place nobody depends on any more: null the coordinates. Free,
    and `haversine.distance_expr` already reads NULL as "unknown distance"
    while explore's bounding box drops the row, so an expired place leaves
    nearby results on its own.

The unit of work is the LOCATION, not the row that points at it. One "Kannur"
serves every user in Kannur, so a town with a thousand players costs one call
per cycle, and the denormalized `latitude`/`longitude` columns that distance
queries actually read are caught up by
``LocationService.propagate_coords``.

Refresh calls bill the Place Details SKU, so they count under sku ``refresh``
and share the ``details`` daily cap: a long refresh run must be able to run out
of the same budget a user-facing search draws on, or the cap is not a cap.

Nothing here is scheduled. The command runs by hand (a Render cron later).
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.db.models import F, Q
from django.utils import timezone

from places.services import google_places_client as google
from places.services.google_places_client import (
    DETAILS_REFRESH_FIELD_MASK,
    GooglePlacesNotConfigured,
    GooglePlacesNotFound,
    GooglePlacesRateLimited,
)
from places.services.places_service import (
    SKU_DETAILS,
    SKU_REFRESH,
    PlacesUnavailable,
    check_budget,
    increment_usage,
)
from services.location.location_service import LocationService
from shared.models import Location

logger = logging.getLogger(__name__)


# Per-location outcomes, which are also the summary's column names.
REFRESHED = "refreshed"
NOT_FOUND = "not_found"
ERROR = "errors"
SKIPPED = "skipped"

# Defaults from section 1.2, used when the setting is absent.
DEFAULT_REFRESH_AFTER_DAYS = 25
DEFAULT_EXPIRE_AFTER_DAYS = 30
DEFAULT_ACTIVE_USER_DAYS = 30


def refresh_after_days():
    return int(
        getattr(
            settings,
            "PLACES_COORDS_REFRESH_AFTER_DAYS",
            DEFAULT_REFRESH_AFTER_DAYS,
        )
    )


def expire_after_days():
    return int(
        getattr(
            settings,
            "PLACES_COORDS_EXPIRE_AFTER_DAYS",
            DEFAULT_EXPIRE_AFTER_DAYS,
        )
    )


def active_user_days():
    return int(
        getattr(settings, "PLACES_ACTIVE_USER_DAYS", DEFAULT_ACTIVE_USER_DAYS)
    )


# ── Who is still using a place (section 6, "Active") ─────────────────────────


def select_active_location_ids():
    """
    The ids of every Location something still depends on. Returns a set.

    Three sources, and the interesting part is what is NOT one:

      * **Profiles of recent users.** ``last_login`` OR ``profile.updated_at``
        inside the window — the OR is not belt-and-braces. Refresh tokens
        rotate for thirty days without touching ``last_login``, and until this
        stage no login path wrote it at all, so a check on ``last_login``
        alone would expire the coordinates of people who use the app daily.
      * **Every org location**, no recency test. An org's branch is a business
        address on a public profile, not a personal preference, and there are
        few enough of them that ageing them out saves nothing worth the risk.
      * **Open recruitments** — draft or active, not deleted. A closed
        recruitment is history; nobody searches it by distance any more.

    **Posts are deliberately absent.** A post needs only the label it already
    stores, so a five-year-old post cannot on its own keep a place alive — and
    posts are the single biggest table pointing at Locations, so counting them
    would make almost everything permanently active and the expiry a no-op.

    A set in memory rather than a subquery: the follow-up queries need it twice
    (once as ``id__in``, once as ``exclude``), the ids are UUIDs of distinct
    real-world places rather than of rows, and this runs from a command, not a
    request.
    """
    from accounts.models import UserProfile
    from organization.models import OrganizationLocation
    from recruitments.models import Recruitment

    cutoff = timezone.now() - timedelta(days=active_user_days())

    active = set()

    active.update(
        UserProfile.objects
        .filter(location_id__isnull=False)
        .filter(Q(user__last_login__gte=cutoff) | Q(updated_at__gte=cutoff))
        .values_list("location_id", flat=True)
    )

    active.update(
        OrganizationLocation.objects
        .filter(location_id__isnull=False)
        .values_list("location_id", flat=True)
    )

    active.update(
        Recruitment.objects
        .filter(
            location_id__isnull=False,
            is_deleted=False,
            status__in=[Recruitment.Status.DRAFT, Recruitment.Status.ACTIVE],
        )
        .values_list("location_id", flat=True)
    )

    return active


def _refreshable():
    """
    Google rows with a place id — the only ones that CAN be refreshed.

    A manual row has nothing to ask Google about, and a google row with a blank
    external_id is a row that predates the picker.
    """
    return Location.objects.filter(
        provider=Location.Provider.GOOGLE
    ).exclude(external_id="")


def locations_needing_refresh(active_ids):
    """Active google Locations whose coordinates are stale or were never set."""
    stale_before = timezone.now() - timedelta(days=refresh_after_days())

    return (
        _refreshable()
        .filter(id__in=active_ids)
        .filter(
            Q(coords_fetched_at__isnull=True)
            | Q(coords_fetched_at__lt=stale_before)
        )
        # Stalest first, and never-fetched ahead of everything — Postgres sorts
        # NULLs LAST by default, which under --limit would leave the rows with
        # no coordinates at all until every merely-old row had been done.
        .order_by(F("coords_fetched_at").asc(nulls_first=True), "id")
    )


def locations_to_expire(active_ids):
    """
    Inactive google Locations still holding coordinates old enough to drop.

    Note the asymmetry with the refresh query, which is section 6.1 as written:
    a NULL ``coords_fetched_at`` counts as stale for REFRESH but not for
    EXPIRY. Nulling coordinates whose age is unknown would be a guess, and the
    row leaves the query for free the moment it is refreshed or re-stamped.

    ``external_id`` is not required here — expiring needs no API call, so a row
    with no place id can still shed coordinates it should not be keeping.
    """
    expire_before = timezone.now() - timedelta(days=expire_after_days())

    return (
        Location.objects
        .filter(provider=Location.Provider.GOOGLE)
        .exclude(id__in=active_ids)
        .filter(Q(latitude__isnull=False) | Q(longitude__isnull=False))
        .filter(coords_fetched_at__lt=expire_before)
        .order_by("coords_fetched_at", "id")
    )


# ── The two operations ───────────────────────────────────────────────────────


def refresh_location(location):
    """
    One Place Details call for one Location, then propagate.

    Returns REFRESHED / NOT_FOUND / ERROR / SKIPPED, and raises
    ``PlacesUnavailable`` for the two conditions that mean STOP THE WHOLE RUN
    rather than "this one failed": the daily cap is spent, Google itself
    answered 429, or there is no API key. A caller that kept going through
    those would spend the rest of the run being refused.

    A ``NOT_FOUND`` place id is left exactly as it is — the row keeps its label
    and its stale coordinates. Google retiring a place id is not a reason to
    lose the town a hundred profiles point at, and the next run will simply try
    again.
    """
    TAG = "CoordsRefreshService.refresh_location"

    if location.provider != Location.Provider.GOOGLE or not location.external_id:
        return SKIPPED

    # Before the call, not after: a cap that stops the request is a cap, and
    # one that only hides the answer is a receipt.
    check_budget(SKU_DETAILS)

    try:
        payload = google.place_details(
            place_id=location.external_id,
            # No session token: there is no autocomplete session to close, and
            # sending one would be a token nothing else in the world shares.
            session_token=None,
            field_mask=DETAILS_REFRESH_FIELD_MASK,
        )
    except GooglePlacesNotConfigured:
        raise PlacesUnavailable("search_unavailable")
    except GooglePlacesNotFound:
        increment_usage(SKU_REFRESH)
        # place_id is safe to log here and nowhere else in this app: it is
        # already stored permanently on the row, this is a background job with
        # no query text or session anywhere near it, and the id is the only
        # thing that makes the line actionable.
        logger.warning(
            f"{TAG} | Place id no longer resolves | "
            f"location={location.id} | place_id={location.external_id}"
        )
        return NOT_FOUND
    except GooglePlacesRateLimited:
        increment_usage(SKU_REFRESH)
        raise PlacesUnavailable("search_unavailable")
    except Exception as e:
        increment_usage(SKU_REFRESH)
        logger.error(
            f"{TAG} | Failed | location={location.id} | {type(e).__name__}"
        )
        return ERROR

    increment_usage(SKU_REFRESH)

    coords = payload.get("location") or {}
    latitude = coords.get("latitude")
    longitude = coords.get("longitude")

    if latitude is None or longitude is None:
        # A 200 with no point. Nothing to write, and overwriting good
        # coordinates with NULL because Google answered oddly is the one
        # outcome worth guarding against.
        logger.error(
            f"{TAG} | Response carried no coordinates | location={location.id}"
        )
        return ERROR

    location.latitude = latitude
    location.longitude = longitude
    location.coords_fetched_at = timezone.now()
    location.save(
        update_fields=["latitude", "longitude", "coords_fetched_at"]
    )

    LocationService.propagate_coords(location)

    return REFRESHED


def expire_location(location):
    """
    Drop the coordinates of a place nobody is using. No API call.

    ``coords_fetched_at`` is deliberately KEPT: it is the record of when the
    point was last real, and it is what decides whether a row that becomes
    active again is refreshed. The row leaves ``locations_to_expire`` anyway,
    because its coordinates are now NULL — so this is idempotent.
    """
    location.latitude = None
    location.longitude = None
    location.save(update_fields=["latitude", "longitude"])

    LocationService.propagate_coords(location)


# ── Login hook (section 6.2) ─────────────────────────────────────────────────


def ensure_fresh_for_user(user):
    """
    Top up the coordinates of the city on ``user``'s profile, at login.

    This is the other half of expiry: a player who stops using Goatza for two
    months has their city's coordinates nulled and drops out of nearby results,
    and this is what puts them back the moment they return — without a job that
    has to guess who is coming back.

    **Never raises.** Every failure path returns False, because the caller is a
    login view and a place lookup is not allowed to be the reason somebody
    cannot sign in. Costs at most one Google call, bounded by the client's 4 s
    timeout.

    Returns True only when coordinates were actually refreshed.
    """
    TAG = "CoordsRefreshService.ensure_fresh_for_user"

    try:
        profile = getattr(user, "profile", None)
        location = getattr(profile, "location", None) if profile else None

        if location is None:
            return False

        if (
            location.provider != Location.Provider.GOOGLE
            or not location.external_id
        ):
            return False

        fresh_since = timezone.now() - timedelta(
            days=refresh_after_days()
        )

        has_coords = (
            location.latitude is not None and location.longitude is not None
        )
        is_fresh = (
            location.coords_fetched_at is not None
            and location.coords_fetched_at >= fresh_since
        )

        if has_coords and is_fresh:
            return False

        return refresh_location(location) == REFRESHED

    except Exception as e:
        # Includes PlacesUnavailable — the cap being spent is a perfectly
        # ordinary reason not to refresh at login, and never a reason to fail
        # one.
        logger.warning(f"{TAG} | Skipped | {type(e).__name__}")
        return False
