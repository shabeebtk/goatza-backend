"""
Give every existing open trial the TrialSession row its dates now live in.

Before this stage a trial had ONE date, stored directly on the recruitment as
``event_date``. It now has a ``TrialSession`` per date, and the recruitment's
``event_date`` / ``trial_end_date`` are DERIVED from those rows. A recruitment
written before the change has no session at all, so both derived columns are
empty and nothing downstream can tell when its trial ends.

*** RUN THIS BEFORE DEPLOYING THE trial_not_over_q CHANGE. ***

``trial_window.trial_not_over_q`` now filters on ``trial_end_date``, and — by
design, so a dateless posting is never hidden — a NULL bound never excludes a
row. Every pre-backfill recruitment has a null ``trial_end_date``. Ship the
query without running this first and every trial that ended months ago walks
straight back into the All tab, search, discover and the public org bundle.

What it does, for every open_trial that has an ``event_date`` and NO sessions:

  1. SESSION   creates exactly ONE TrialSession from the stored date:
                 date        the event_date's calendar day in THAT
                             RECRUITMENT'S OWN timezone — the same zone
                             _sync_trial_window will read the new row back
                             in, so the derived instants land where they
                             were read from
                 start_time  its time — or NULL when the stored time is the
                             23:59 or 00:00 date-only sentinel the wizard
                             writes for a trial with no time (see the
                             frontend's wizardDate.ts)
                 venue       venue_name, venue_link, location, city, latitude
                             and longitude copied off the recruitment
  2. WINDOW    runs ``RecruitmentService._sync_trial_window`` on it, so
               ``event_date`` is re-derived (it lands on the same instant) and
               ``trial_end_date`` is finally set.

Characteristics:
  * Idempotent  — a recruitment that already has ANY session (cancelled ones
                  included) is skipped, so a second run changes nothing and it
                  never fights an org that has since edited its dates by hand.
                  Safe to re-run any time.
  * Batched     — keyset pages of --batch-size rows, each written in one
                  transaction with its rows locked.
  * Flat memory — rows are read one batch at a time.
  * --dry-run   — reads only and writes nothing, then prints the same report.

    python manage.py backfill_trial_sessions --dry-run          # report only
    python manage.py backfill_trial_sessions
    python manage.py backfill_trial_sessions --batch-size 100   # smaller batches
"""

from datetime import time

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.recruitments.models import Recruitment, TrialSession
from apps.recruitments.services.recruitment_service import RecruitmentService

# The two stored times that mean "no time was chosen", not "starts at 23:59".
# 23:59 is what the wizard writes today; 00:00 is what legacy rows carry.
DATE_ONLY_SENTINELS = (time(23, 59), time(0, 0))


def _id_batches(queryset, batch_size):
    """Keyset pages of ids — flat memory, and stable under concurrent edits."""
    last_id = None
    while True:
        page = queryset.order_by("id")
        if last_id is not None:
            page = page.filter(id__gt=last_id)
        ids = list(page.values_list("id", flat=True)[:batch_size])
        if not ids:
            return
        yield ids
        last_id = ids[-1]


class Command(BaseCommand):
    help = (
        "Create the one TrialSession each pre-existing open trial's "
        "event_date stands for, and derive its trial_end_date. MUST be run "
        "before the trial_not_over_q change is deployed."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Read only. Report what would change and write nothing.",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=500,
            help="Rows per transaction (default 500).",
        )

    def handle(self, *args, **options):
        self.dry_run = options["dry_run"]
        self.batch_size = max(1, options["batch_size"])

        self.created = 0
        self.timed = 0
        self.date_only = 0
        self.already_had_sessions = 0
        self.no_event_date = 0

        if self.dry_run:
            self.stdout.write(self.style.WARNING(
                "DRY RUN — reading only, nothing will be written."
            ))

        self._count_skips()
        self._backfill()
        self._report()

    # -- what this command deliberately leaves alone ------------------

    def _count_skips(self):
        """
        The two reasons an open trial is passed over, counted up front so the
        report can account for every row rather than only the ones it touched.
        """
        open_trials = Recruitment.objects.filter(
            recruitment_type=Recruitment.Type.OPEN_TRIAL
        )

        self.no_event_date = open_trials.filter(
            event_date__isnull=True
        ).count()

        self.already_had_sessions = open_trials.filter(
            event_date__isnull=False,
            sessions__isnull=False,
        ).distinct().count()

    # -- the one pass -------------------------------------------------

    def _backfill(self):
        queryset = Recruitment.objects.filter(
            recruitment_type=Recruitment.Type.OPEN_TRIAL,
            event_date__isnull=False,
            sessions__isnull=True,
        )
        processed = 0

        for ids in _id_batches(queryset, self.batch_size):
            with transaction.atomic():
                rows = Recruitment.objects.filter(id__in=ids)
                if not self.dry_run:
                    rows = rows.select_for_update()

                for recruitment in rows:
                    # Re-checked under the lock: another batch, or an org
                    # editing right now, may have given it a date already.
                    if recruitment.sessions.exists():
                        continue

                    # Read back in the recruitment's OWN zone. Reading
                    # every row on one clock is what would shift a London
                    # trial's date by a day and its time by five and a half
                    # hours, permanently, in the row this command creates.
                    local = recruitment.event_date.astimezone(
                        recruitment.zoneinfo
                    )
                    start_time = local.time().replace(microsecond=0)

                    if start_time in DATE_ONLY_SENTINELS:
                        start_time = None
                        self.date_only += 1
                    else:
                        self.timed += 1

                    self.created += 1

                    if self.dry_run:
                        continue

                    TrialSession.objects.create(
                        recruitment=recruitment,
                        date=local.date(),
                        start_time=start_time,
                        venue_name=recruitment.venue_name,
                        venue_link=recruitment.venue_link,
                        location=recruitment.location,
                        city=recruitment.city,
                        latitude=recruitment.latitude,
                        longitude=recruitment.longitude,
                    )

                    # The point of the whole command: trial_end_date. Also
                    # re-derives event_date, which lands on the same instant
                    # it was read from.
                    RecruitmentService._sync_trial_window(recruitment)

            processed += len(ids)
            self.stdout.write(f"  sessions: processed {processed}")

    # -- report -------------------------------------------------------

    def _line(self, label, value, width=52):
        self.stdout.write(f"  {label:<{width}}: {value}")

    def _report(self):
        verb = (
            "would be created — dry-run, nothing written"
            if self.dry_run
            else "created"
        )

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING(f"SESSIONS ({verb})"))
        self._line("recruitments given a trial date", self.created)
        self._line("  ... from a timed event_date", self.timed)
        self._line("  ... from a date-only event_date (23:59 / 00:00)",
                   self.date_only)

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("SKIPPED"))
        self._line("open trials that already had dates",
                   self.already_had_sessions)
        self._line("open trials with no event_date to convert",
                   self.no_event_date)

        self.stdout.write("")
        self.stdout.write(self.style.WARNING(
            "  REMINDER: trial_window.trial_not_over_q now filters on\n"
            "  trial_end_date, and a NULL bound never excludes a row. This\n"
            "  command MUST have run before that change is deployed, or every\n"
            "  ended trial reappears in the player-facing lists."
        ))

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(
            "Done (dry-run — no writes)." if self.dry_run else "Done."
        ))
