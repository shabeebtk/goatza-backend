"""
Run the coordinate lifecycle (docs/PLACES_MIGRATION.md section 6.1).

    python manage.py refresh_place_coords --dry-run     # report only
    python manage.py refresh_place_coords
    python manage.py refresh_place_coords --limit 50    # cap the Google calls
    python manage.py refresh_place_coords --sleep-ms 100

Two passes over ``shared.Location``:

  1. **Refresh** every ACTIVE google place whose coordinates are stale or were
     never fetched — one Place Details call each (``location`` field mask, no
     session token), then propagate to the denormalized columns.
  2. **Expire** every INACTIVE google place still holding coordinates old
     enough to drop. No API call.

Then one summary line: refreshed / expired / not_found / errors / google_calls.

By hand for now; a Render cron job runs the same command later. Deliberately
not Celery — this is one query, a handful of HTTP calls and an update, and a
broker would be more moving parts than the job has work.

Safe to interrupt and safe to re-run: each location is committed as it is
handled, and a half-finished run simply leaves the rest stale for next time.
"""

import time

from django.core.management.base import BaseCommand

from places.services.coords_refresh_service import (
    ERROR,
    NOT_FOUND,
    REFRESHED,
    SKIPPED,
    expire_after_days,
    expire_location,
    locations_needing_refresh,
    locations_to_expire,
    refresh_after_days,
    refresh_location,
    select_active_location_ids,
)
from places.services.places_service import (
    SKU_REFRESH,
    PlacesUnavailable,
    get_usage,
)


class Command(BaseCommand):
    help = (
        "Refresh coordinates for active Google places and expire them for "
        "inactive ones"
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would happen. No Google calls, no writes.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help=(
                "Cap the REFRESH pass at N locations (the pass that spends "
                "money). Expiry is free and always runs in full."
            ),
        )
        parser.add_argument(
            "--sleep-ms",
            type=int,
            default=50,
            help=(
                "Pause between Google calls, in milliseconds (default 50). "
                "Politeness, not rate limiting — the daily cap is the guard."
            ),
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        limit = options["limit"]
        sleep_seconds = max(0, options["sleep_ms"]) / 1000.0

        counts = {REFRESHED: 0, "expired": 0, NOT_FOUND: 0, ERROR: 0}

        # google_calls is read off the usage counter rather than tallied in the
        # loop: the counter is what the daily cap and `places_usage` read, so a
        # summary derived from it can never disagree with them, and it stays
        # right for the calls that end the run (a Google 429 bills a request
        # and then breaks out of the loop).
        calls_before = get_usage(SKU_REFRESH)

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"Place coordinates{' (dry-run)' if dry_run else ''}"
        ))
        self.stdout.write(
            f"  refresh after {refresh_after_days()}d · "
            f"expire after {expire_after_days()}d"
        )

        active_ids = select_active_location_ids()
        self.stdout.write(f"  active locations: {len(active_ids)}")

        # ── 1. Refresh ───────────────────────────────────────────────────────

        to_refresh = locations_needing_refresh(active_ids)

        if limit is not None:
            to_refresh = to_refresh[:max(0, limit)]

        # Materialised before the loop: refreshing a row rewrites the very
        # column the queryset filters and orders on, so a lazy queryset would
        # be re-evaluated mid-iteration against a moving target.
        to_refresh = list(to_refresh)
        self.stdout.write(f"  to refresh: {len(to_refresh)}")

        stopped_early = None

        for location in to_refresh:
            if dry_run:
                self.stdout.write(
                    f"    would refresh {location.name or '(unnamed)'} "
                    f"[{location.id}]"
                )
                counts[REFRESHED] += 1
                continue

            try:
                outcome = refresh_location(location)
            except PlacesUnavailable:
                # The daily Details cap is spent, Google answered 429, or there
                # is no key. Every remaining call would be refused, so stop
                # rather than walk the rest of the list into the same wall.
                stopped_early = (
                    "daily Details cap reached or Google unavailable"
                )
                break

            if outcome == SKIPPED:
                continue

            if outcome == REFRESHED:
                counts[REFRESHED] += 1
            elif outcome == NOT_FOUND:
                counts[NOT_FOUND] += 1
            else:
                counts[ERROR] += 1

            if sleep_seconds:
                time.sleep(sleep_seconds)

        # ── 2. Expire ────────────────────────────────────────────────────────

        to_expire = list(locations_to_expire(active_ids))
        self.stdout.write(f"  to expire: {len(to_expire)}")

        for location in to_expire:
            if dry_run:
                self.stdout.write(
                    f"    would expire {location.name or '(unnamed)'} "
                    f"[{location.id}]"
                )
                counts["expired"] += 1
                continue

            try:
                expire_location(location)
                counts["expired"] += 1
            except Exception as e:
                self.stderr.write(
                    f"    expire failed for {location.id}: {type(e).__name__}"
                )
                counts[ERROR] += 1

        # ── 3. Summary ───────────────────────────────────────────────────────

        if stopped_early:
            self.stdout.write(self.style.WARNING(
                f"  stopped early: {stopped_early}"
            ))

        summary = (
            f"refreshed={counts[REFRESHED]} "
            f"expired={counts['expired']} "
            f"not_found={counts[NOT_FOUND]} "
            f"errors={counts[ERROR]} "
            f"google_calls={get_usage(SKU_REFRESH) - calls_before}"
        )

        self.stdout.write("")
        self.stdout.write(
            self.style.WARNING(summary)
            if counts[ERROR] or stopped_early
            else self.style.SUCCESS(summary)
        )
