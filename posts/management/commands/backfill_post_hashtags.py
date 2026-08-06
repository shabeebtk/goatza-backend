"""
Backfill PostHashtag rows for posts written before hashtags were parsed.

Nothing created hashtag rows until the post_content_service write path landed,
so every pre-existing "#football" is invisible to search. This walks the posts
whose body contains a "#" and runs the same sync the create/update views use.

Characteristics:
  * Idempotent  — sync_post_content diffs against what's already there, so a
                  second run creates nothing new. Safe to re-run any time.
  * Flat memory — iterator() streams rows instead of loading every post.
  * Batched     — each batch commits in one transaction.

Run once after deploy:

    python manage.py backfill_post_hashtags
    python manage.py backfill_post_hashtags --dry-run          # report only
    python manage.py backfill_post_hashtags --batch-size 100   # smaller batches
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from posts.models import Post, PostHashtag
from posts.services.post_content_service import sync_post_content


class Command(BaseCommand):
    help = "Create PostHashtag rows for existing posts whose content has hashtags"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would change without writing to the database.",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=500,
            help="Posts processed per transaction (default 500).",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        batch_size = max(1, options["batch_size"])

        # A body with no "#" can never yield a tag, so it is not worth a query.
        # Soft-deleted posts are skipped — they are invisible to search anyway.
        queryset = (
            Post.objects
            .filter(is_deleted=False, content__contains="#")
            .only("id", "content")
            .order_by("id")
        )

        before = PostHashtag.objects.count()

        self.stdout.write(self.style.WARNING(
            f"Backfilling hashtags{' (dry-run)' if dry_run else ''}..."
        ))

        processed = 0
        batch = []

        for post in queryset.iterator(chunk_size=batch_size):
            batch.append(post)
            if len(batch) >= batch_size:
                processed += self._flush(batch, dry_run)
                self.stdout.write(f"  processed {processed}")
                batch = []

        if batch:
            processed += self._flush(batch, dry_run)

        created = PostHashtag.objects.count() - before

        self.stdout.write(self.style.SUCCESS(
            f"Done. processed={processed}, hashtag_rows_created={created}"
            f"{' (dry-run — no writes)' if dry_run else ''}."
        ))

    def _flush(self, batch, dry_run):
        if dry_run:
            return len(batch)

        with transaction.atomic():
            for post in batch:
                sync_post_content(post)
        return len(batch)
