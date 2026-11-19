"""
Delete enquiries closed (joined / not interested) more than 365 days ago.

An enquiry holds a family's phone number and a child's name and age. Once the
org has finished with it, a year is long enough to look back on; after that it
goes. Open enquiries (new / contacted) are never touched, however old.

Beat runs it daily at 03:45 IST (``enquiries.purge_closed_enquiries``). By hand:

    python manage.py purge_closed_enquiries
    python manage.py purge_closed_enquiries --dry-run

Batches of 500, each its own statement, so a large backlog never holds one
long transaction. IDEMPOTENT: a second run finds nothing past the cut-off.
"""

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.enquiries.models import Enquiry

RETENTION_DAYS = 365
BATCH_SIZE = 500


def purge_closed_enquiries(now=None, batch_size=BATCH_SIZE) -> int:
    cutoff = (now or timezone.now()) - timedelta(days=RETENTION_DAYS)
    deleted = 0
    while True:
        ids = list(
            Enquiry.objects
            .filter(closed_at__isnull=False, closed_at__lt=cutoff)
            .values_list("id", flat=True)[:batch_size]
        )
        if not ids:
            return deleted
        deleted += Enquiry.objects.filter(id__in=ids).delete()[0]


class Command(BaseCommand):
    help = f"Delete enquiries closed more than {RETENTION_DAYS} days ago."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Count what would be deleted without deleting it.",
        )

    def handle(self, *args, **options):
        if options["dry_run"]:
            cutoff = timezone.now() - timedelta(days=RETENTION_DAYS)
            count = Enquiry.objects.filter(
                closed_at__isnull=False, closed_at__lt=cutoff
            ).count()
            self.stdout.write(f"Would delete {count} closed enquiries (dry run).")
            return

        deleted = purge_closed_enquiries()
        self.stdout.write(self.style.SUCCESS(f"Closed enquiries deleted: {deleted}"))
