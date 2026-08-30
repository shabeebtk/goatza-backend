"""
Print today's and yesterday's Google Places usage (section 4.4).

    python manage.py places_usage
    python manage.py places_usage --days 7    # further back, if the TTL allows

Reads the same cache keys the circuit breaker writes, so what it prints is
exactly what the breaker will compare against the caps — no second source of
truth.

The counters live in the cache with a 48 h TTL, so only today and yesterday are
reliably present; ``--days`` beyond that prints zeros for days that have
expired, which is indistinguishable from a genuinely quiet day. For real
history, read Google Cloud console metrics.

Nothing here is per-user or per-query: these are call counts, which is all the
server keeps (section 3 rule 7).
"""

from django.core.management.base import BaseCommand

from places.services.places_service import (
    SKU_AUTOCOMPLETE,
    SKU_DETAILS,
    SKU_REFRESH,
    caps,
    usage_report,
)


class Command(BaseCommand):
    help = "Print today's and yesterday's Google Places API usage counters"

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=1,
            help=(
                "How many days BEFORE today to include (default 1 = today "
                "and yesterday). Counters older than 48 h have expired and "
                "print as 0."
            ),
        )

    def handle(self, *args, **options):
        days_back = max(0, options["days"])
        configured = caps()
        report = usage_report(days_back=days_back)

        self.stdout.write(self.style.MIGRATE_HEADING("Google Places usage"))
        self.stdout.write(
            f"  caps: autocomplete={configured[SKU_AUTOCOMPLETE]}/day  "
            f"details={configured[SKU_DETAILS]}/day "
            f"(details + refresh share it)"
        )
        self.stdout.write("")

        header = (
            f"  {'date':<12}{'autocomplete':>14}{'details':>10}"
            f"{'refresh':>10}{'details+refresh':>18}"
        )
        self.stdout.write(header)
        self.stdout.write(f"  {'-' * (len(header) - 2)}")

        # usage_report keys are ordered today-first; newest at the top reads
        # the way anybody running this actually wants it.
        for day, counts in report.items():
            autocomplete = counts[SKU_AUTOCOMPLETE]
            details = counts[SKU_DETAILS]
            refresh = counts[SKU_REFRESH]
            billed_details = details + refresh

            line = (
                f"  {day.isoformat():<12}{autocomplete:>14}{details:>10}"
                f"{refresh:>10}{billed_details:>18}"
            )

            over_cap = (
                autocomplete >= configured[SKU_AUTOCOMPLETE]
                or billed_details >= configured[SKU_DETAILS]
            )

            self.stdout.write(
                self.style.WARNING(line) if over_cap else line
            )

        self.stdout.write("")
