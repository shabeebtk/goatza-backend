"""
The evening-before reminder: tell confirmed players their trial is tomorrow.

Run HOURLY by cron. There is NO GLOBAL CLOCK TO GATE ON any more: a trial
carries its own timezone, so "is it 6pm yet" is a question about the VENUE
and every run has to ask it per row. 6pm in London is 10:30pm in Kochi; a
command that exited early on one country's clock either sent London's
reminders at lunchtime or never sent them at all.

So this is the one place in the recruitment code that still reads timezones
per row — because the thing being compared is not a stored instant but the
CURRENT hour, and that differs per recruitment at the same moment. Each hourly
pass loads the candidate applications and filters each one by its own
recruitment's zone; outside 18:00–23:59 THERE, a row is simply skipped and
picked up by a later run.

Hourly rather than a single daily cron because a cron that fires once a day
has exactly one chance to run: a deploy, a restart or a five-minute outage at
18:00 loses the whole evening's reminders, and nobody finds out until the
players do not turn up. With per-venue windows that is now load-bearing
rather than belt-and-braces — an hour that is "too early" for a London trial
is the right hour for an Indian one.

WHY NOT eta/countdown ON A TASK. CLAUDE.md is explicit: on the Redis transport
anything outstanding longer than the visibility timeout is redelivered — a
duplicate reminder — and a trial whose date changed leaves a stale job nobody
can recall. So the schedule is dumb and the QUERY is smart: this selects the
rows that are due, right now, from the database.

WHO GETS ONE: a ``trial_confirmed`` application on an ACTIVE recruitment whose
trial date is TOMORROW — the player's OWN chosen session in choose_one mode,
otherwise the first non-cancelled session. A player who picked the Kannur round
is not reminded the night before Kochi.

IDEMPOTENT THROUGH ONE COLUMN. ``trial_reminder_sent_at`` is stamped as each
reminder goes out and stamped rows are not selected again, so the other
twenty-three runs of the day are no-ops and a re-run costs nothing. This is
what CLAUDE.md means by "record what was done and make the second run a
no-op" — and with the early exit gone it is the ONLY thing standing between
twenty-four hourly passes and twenty-four reminders. Do not touch it.

AND IF THE DATE MOVES, the stamp is CLEARED — by
``RecruitmentService._sync_trial_window``, not here — so the new date gets its
own reminder. A reminder for a date that no longer exists is worse than none.

Sending is SYNCHRONOUS: the blocking ``utils.emails.send_email``, never
``send_email_async``, for the same reason the announcement outbox drains this
way. A thread per email is what a command exists to avoid.

HOW IT RUNS

  Render Cron:
      0 * * * *  python manage.py send_trial_reminders

  Celery beat (later, no code change): the task in
  ``apps/recruitments/tasks.py`` calls this same command.

    python manage.py send_trial_reminders --dry-run
    python manage.py send_trial_reminders --now "2026-10-11T19:00+05:30"
"""

from collections import Counter
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.notifications.services.notification_service import NotificationService
from apps.recruitments.models import Recruitment, RecruitmentApplication
from utils.transactional_emails import send_trial_reminder_email

# Nothing goes out before this hour AT THE VENUE. An 8am "your trial is
# tomorrow" is not a reminder, it is a day's notice — and 8am is a different
# instant in London and in Kochi, which is why this is checked per row.
REMIND_FROM_HOUR = 18

# Rows per transaction, for the same reason the announcement drain chunks:
# the lock is held across a blocking email send.
CHUNK_SIZE = 25


class Command(BaseCommand):
    help = (
        "Remind confirmed applicants whose trial is tomorrow. Run hourly; "
        "each reminder goes out from 18:00 in its own trial's timezone."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be sent and write nothing.",
        )
        parser.add_argument(
            "--now",
            type=str,
            default=None,
            help="Pretend it is this instant (ISO 8601). Testing only.",
        )

    def handle(self, *args, **options):
        self.dry_run = options["dry_run"]
        self.counts = Counter()

        now = timezone.now()
        if options["now"]:
            parsed = parse_datetime(options["now"])
            if parsed is None:
                self.stderr.write("--now is not a valid ISO 8601 datetime")
                return
            now = parsed

        # The instant, kept as the instant. There is no single local clock to
        # reduce it to any more — every row turns it into its own venue's
        # hour and its own "tomorrow" (see _due_day_for).
        self.now = now
        self.stdout.write(f"  evaluating trials as at {now:%Y-%m-%d %H:%M %Z}")

        if self.dry_run:
            self.stdout.write(self.style.WARNING(
                "DRY RUN — nothing is sent and nothing is stamped."
            ))

        self._send_all()
        self._report()

    # -- who is due ---------------------------------------------------

    def _due(self):
        """
        Confirmed applications on live recruitments, not yet reminded.

        The DATE test is done per row rather than in SQL: which session
        applies depends on the recruitment's session_mode and on the
        applicant's own choice, and expressing that as a query would mean two
        branches that can disagree. The SAME now goes for the 18:00 gate,
        which depends on the recruitment's own timezone and so has no single
        SQL answer at all. The candidate set is already small — one evening's
        confirmed applicants, worldwide — so the filtering that matters
        (status, active, unstamped) is in SQL and the clock comparisons are
        not.
        """
        return (
            RecruitmentApplication.objects
            .filter(
                status=RecruitmentApplication.Status.TRIAL_CONFIRMED,
                trial_reminder_sent_at__isnull=True,
                recruitment__status=Recruitment.Status.ACTIVE,
                recruitment__is_deleted=False,
                recruitment__recruitment_type=Recruitment.Type.OPEN_TRIAL,
            )
            .select_related(
                "recruitment__organization",
                "applicant",
                "session",
                "age_category",
            )
            .order_by("id")
        )

    def _session_for(self, application):
        """
        The date THIS applicant is expected on.

        choose_one: the one they picked, and nothing else — reminding a
        Kannur player the night before Kochi is worse than silence.
        all mode: the first non-cancelled session, which is where the trial
        starts and what ``event_date`` already mirrors.
        """
        recruitment = application.recruitment

        if recruitment.session_mode == Recruitment.SessionMode.CHOOSE_ONE:
            session = application.session
            if not session or session.is_cancelled:
                return None
            # The chosen row arrives through select_related("session"), so
            # its own parent FK is unloaded; hand it the recruitment we
            # already have rather than letting ``starts_at`` fetch it again.
            session.recruitment = recruitment
            return session

        return (
            recruitment.sessions
            .filter(is_cancelled=False)
            .order_by("date", "start_time", "display_order")
            .first()
        )

    def _due_day_for(self, recruitment):
        """
        The calendar date this recruitment is currently reminding FOR, or
        None while it is still too early at its venue.

        THE PER-ROW CLOCK, and the only place one is read. "Tomorrow" is
        tomorrow where the trial is: at 19:00 UTC it is 00:30 in Kochi (so
        an Indian trial is past its evening and the date it would remind for
        has already arrived) and 20:00 in London (so a London trial is in
        its window, reminding for the next day).
        """
        local_now = self.now.astimezone(recruitment.zoneinfo)
        if local_now.hour < REMIND_FROM_HOUR:
            return None
        return (local_now + timedelta(days=1)).date()

    # -- the pass -----------------------------------------------------

    def _send_all(self):
        ids = list(self._due().values_list("id", flat=True))

        for start in range(0, len(ids), CHUNK_SIZE):
            chunk = ids[start: start + CHUNK_SIZE]
            with transaction.atomic():
                rows = (
                    self._due()
                    .filter(id__in=chunk)
                    .select_for_update(of=("self",))
                )
                for application in rows:
                    self._send_one(application)

    def _send_one(self, application):
        recruitment = application.recruitment

        # Before 18:00 at the venue there is nothing to do for THIS row —
        # and nothing is stamped, so a later pass in the same evening picks
        # it up. That is what replaces the old global early exit.
        trial_day = self._due_day_for(recruitment)
        if trial_day is None:
            self.counts["too_early_there"] += 1
            return

        session = self._session_for(application)

        if session is None or session.date != trial_day:
            self.counts["not_tomorrow"] += 1
            return

        self.counts["due"] += 1

        if self.dry_run:
            return

        try:
            NotificationService.trial_reminder(
                actor_org=recruitment.organization,
                recipient_user=application.applicant,
                recruitment=recruitment,
                application=application,
                session=session,
            )
            self.counts["notified"] += 1
        except Exception as exc:
            self.counts["notification_failed"] += 1
            self.stderr.write(f"  notification failed: {exc}")

        try:
            if send_trial_reminder_email(
                application=application, session=session, blocking=True
            ):
                self.counts["emailed"] += 1
            else:
                self.counts["email_skipped"] += 1
        except Exception as exc:
            self.counts["email_failed"] += 1
            self.stderr.write(f"  email failed: {exc}")

        # STAMPED EVEN IF A CHANNEL FAILED. The alternative is retrying every
        # hour until midnight, which turns one bad address into six reminders
        # for everyone else who shares the row's fate. At-most-once is the
        # right trade for a reminder, same as the applicant alert.
        application.trial_reminder_sent_at = timezone.now()
        application.save(update_fields=["trial_reminder_sent_at"])

    # -- report -------------------------------------------------------

    def _report(self):
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("THIS RUN"))
        if not self.counts:
            self.stdout.write("  nobody due")
        for key in sorted(self.counts):
            self.stdout.write(f"  {key.replace('_', ' '):<28}: {self.counts[key]}")

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(
            "Done (dry-run — nothing sent)." if self.dry_run else "Done."
        ))
