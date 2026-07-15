"""
Backfill intrinsic width/height (and video duration) on existing PostMedia rows.

Dimensions are read straight from the storage provider (Cloudinary) via the
same server-side path used on upload — the client is never trusted.

Characteristics:
  * Batched      — processes a bounded number of rows per DB write.
  * Resumable    — only targets rows still missing dimensions and commits each
                   batch, so an interrupted run continues cleanly on re-run.
  * Skips filled — rows that already have both width and height are ignored.
  * Logs failures — every row whose dimensions can't be extracted is reported;
                    it is left NULL (the client falls back to a square ratio)
                    and will be retried on the next run.

Run manually (NOT wired into deploy):

    python manage.py backfill_media_dimensions                 # backfill all missing
    python manage.py backfill_media_dimensions --dry-run       # report only, no writes
    python manage.py backfill_media_dimensions --batch-size 50 # smaller DB batches
    python manage.py backfill_media_dimensions --limit 500     # cap this run
"""

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Q

from posts.models import PostMedia
from services.storage.factory import get_storage_service


class Command(BaseCommand):
    help = "Backfill width/height (and video duration) on existing PostMedia rows"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would change without writing to the database.",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=100,
            help="Rows written per DB batch (default 100).",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Maximum number of rows to process in this run.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        batch_size = max(1, options["batch_size"])
        limit = options["limit"]

        storage = get_storage_service()

        # Target only rows still missing dimensions (skip already-filled).
        # Snapshot the ids up front so a persistent extraction failure can't
        # loop forever within a single run; a fresh run re-snapshots what's left.
        target_qs = (
            PostMedia.objects
            .filter(Q(width__isnull=True) | Q(height__isnull=True))
            .order_by("id")
        )
        target_ids = list(target_qs.values_list("id", flat=True))
        if limit is not None:
            target_ids = target_ids[:limit]

        total = len(target_ids)
        if total == 0:
            self.stdout.write(self.style.SUCCESS("Nothing to backfill — all media have dimensions."))
            return

        self.stdout.write(self.style.WARNING(
            f"Backfilling dimensions for {total} media row(s)"
            f"{' (dry-run)' if dry_run else ''}..."
        ))

        updated = 0
        failed = 0
        processed = 0

        for start in range(0, total, batch_size):
            batch_ids = target_ids[start:start + batch_size]
            rows = PostMedia.objects.filter(id__in=batch_ids)

            to_update = []
            for media in rows:
                processed += 1
                meta = storage.get_media_metadata(media.public_id, media.media_type)
                width = meta.get("width")
                height = meta.get("height")

                if width and height:
                    media.width = width
                    media.height = height
                    if meta.get("duration") is not None:
                        media.duration = meta["duration"]
                    to_update.append(media)
                    updated += 1
                else:
                    failed += 1
                    self.stderr.write(self.style.ERROR(
                        f"  ! Could not extract dimensions | media_id={media.id} | "
                        f"public_id={media.public_id} | type={media.media_type}"
                    ))

            if to_update and not dry_run:
                with transaction.atomic():
                    PostMedia.objects.bulk_update(
                        to_update, ["width", "height", "duration"]
                    )

            self.stdout.write(
                f"  processed {processed}/{total} "
                f"(updated={updated}, failed={failed})"
            )

        summary = (
            f"Done. processed={processed}, updated={updated}, failed={failed}"
            f"{' (dry-run — no writes)' if dry_run else ''}."
        )
        self.stdout.write(self.style.SUCCESS(summary))
