"""
Pre-generate the canonical transcoded derivative for videos uploaded before
eager generation was wired into the create paths.

New videos are covered automatically (posts/highlights/chat all call
``ensure_video_derivatives`` on create). This command closes the gap for
everything already in the database: without it, every legacy clip still pays a
cold-start transcode the first time somebody opens it.

Characteristics:
  * Read-only    — writes nothing to our database; the work happens at
                   Cloudinary. Nothing to roll back, nothing to corrupt.
  * Deduped      — a promoted highlight shares its source post's public_id, so
                   the same asset would otherwise be requested twice.
  * Safe to re-run — ``explicit`` on an already-derived asset is a no-op, and
                   the command has no state of its own. Re-run it after any
                   change to the transformation string.
  * Rate-limited — a small sleep between calls; Cloudinary's upload/admin API
                   will start rejecting a tight loop over thousands of assets.
  * Resumable    — an interrupted run just gets re-run; already-derived assets
                   cost one cheap call each.

Run manually (NOT wired into deploy):

    python manage.py backfill_video_derivatives                  # everything
    python manage.py backfill_video_derivatives --dry-run        # report only
    python manage.py backfill_video_derivatives --limit 500      # cap this run
    python manage.py backfill_video_derivatives --sleep 0.5      # gentler
    python manage.py backfill_video_derivatives --source posts   # one source
"""

import time

from django.core.management.base import BaseCommand

from highlights.models import Highlight
from messaging.models import Message
from posts.models import PostMedia
from services.storage.factory import get_storage_service

SOURCES = ("posts", "highlights", "chat")


class Command(BaseCommand):
    help = (
        "Pre-generate Cloudinary video derivatives for existing post, highlight "
        "and chat videos"
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be requested without calling Cloudinary.",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=100,
            help="public_ids per progress report (default 100).",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Maximum number of public_ids to process in this run.",
        )
        parser.add_argument(
            "--sleep",
            type=float,
            default=0.2,
            help=(
                "Seconds to wait between Cloudinary calls (default 0.2). "
                "Raise it if you start seeing rate-limit errors."
            ),
        )
        parser.add_argument(
            "--source",
            choices=SOURCES,
            action="append",
            default=None,
            help=(
                "Limit to one source (repeatable). Default: all of "
                + ", ".join(SOURCES)
                + "."
            ),
        )

    # ── collection ───────────────────────────────────────────────

    def _collect(self, sources):
        """
        Every distinct video public_id, with the source(s) it came from.

        Ordered by first appearance so a --limit'd run is reproducible, and
        deduped because a promoted highlight carries its post's public_id
        verbatim — requesting the same derivative twice is harmless but wastes
        a rate-limited call.
        """
        found = {}

        def add(public_id, source):
            if not public_id:
                return
            found.setdefault(public_id, []).append(source)

        if "posts" in sources:
            rows = (
                PostMedia.objects
                .filter(media_type=PostMedia.MediaType.VIDEO)
                .order_by("id")
                .values_list("public_id", flat=True)
            )
            for public_id in rows.iterator():
                add(public_id, "posts")

        if "highlights" in sources:
            rows = (
                Highlight.objects
                .filter(is_deleted=False)
                .order_by("id")
                .values_list("public_id", flat=True)
            )
            for public_id in rows.iterator():
                add(public_id, "highlights")

        if "chat" in sources:
            # Deleted messages are skipped: the row survives a soft delete but
            # nobody can play it, so there is nothing to warm up.
            rows = (
                Message.objects
                .filter(message_type=Message.Type.VIDEO, is_deleted=False)
                .exclude(media_public_id="")
                .order_by("id")
                .values_list("media_public_id", flat=True)
            )
            for public_id in rows.iterator():
                add(public_id, "chat")

        return found

    # ── run ──────────────────────────────────────────────────────

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        batch_size = max(1, options["batch_size"])
        limit = options["limit"]
        sleep_seconds = max(0.0, options["sleep"])
        sources = tuple(options["source"] or SOURCES)

        storage = get_storage_service()

        found = self._collect(sources)
        public_ids = list(found.keys())

        counts = {source: 0 for source in SOURCES}
        for origins in found.values():
            for origin in origins:
                counts[origin] += 1

        total_refs = sum(counts.values())
        duplicates = total_refs - len(public_ids)

        if limit is not None and limit < len(public_ids):
            self.stdout.write(self.style.WARNING(
                f"  --limit {limit}: processing {limit} of {len(public_ids)} "
                f"public_id(s); re-run without --limit to finish the rest."
            ))
            public_ids = public_ids[:limit]

        total = len(public_ids)
        if total == 0:
            self.stdout.write(self.style.SUCCESS(
                "Nothing to do — no video assets found."
            ))
            return

        self.stdout.write(self.style.WARNING(
            f"Requesting video derivatives for {total} unique public_id(s) "
            f"[posts={counts['posts']}, highlights={counts['highlights']}, "
            f"chat={counts['chat']}, deduped={duplicates}]"
            f"{' (dry-run)' if dry_run else ''}..."
        ))

        processed = 0

        for public_id in public_ids:
            processed += 1

            if not dry_run:
                # Best-effort by design: a failure is logged inside the storage
                # service and the next asset still gets its turn. Re-running the
                # command retries whatever didn't take.
                storage.ensure_video_derivatives(public_id)
                if sleep_seconds:
                    time.sleep(sleep_seconds)

            if processed % batch_size == 0 or processed == total:
                self.stdout.write(f"  processed {processed}/{total}")

        summary = (
            f"Done. unique_public_ids={total}, "
            f"posts={counts['posts']}, highlights={counts['highlights']}, "
            f"chat={counts['chat']}, deduped={duplicates}"
            f"{' (dry-run — no Cloudinary calls made)' if dry_run else ''}."
        )
        self.stdout.write(self.style.SUCCESS(summary))
        if not dry_run:
            self.stdout.write(
                "Derivatives are generated asynchronously — allow a few minutes "
                "before checking a clip."
            )
