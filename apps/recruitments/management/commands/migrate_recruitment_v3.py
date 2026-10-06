"""
Move existing recruitments and applications onto the v3 status model.

Migration recruitments/0011 added trial_confirmed / not_shortlisted /
not_selected and stopped orgs from setting invited and rejected. Rows written
before it still carry the old, overloaded values — most visibly `selected` set
days before a trial, which is an org saying "come along", not "you made it".
This command rewrites them, in this order:

  1. TYPES     scholarship / direct_recruitment → open_trial when it has an
               event_date, player_looking when it has none. A scholarship also
               gains a "Scholarship" benefit row, so the posting still says so.
  2. STATUSES  invited → trial_confirmed. selected / rejected are resolved by
               WHEN the org set them, against the trial day, by calendar day
               IN EACH RECRUITMENT'S OWN TIMEZONE:
                 open_trial, set before the trial day   → trial_confirmed /
                                                          not_shortlisted
                 open_trial, set on/after it, or with
                 no event_date; every other type        → selected /
                                                          not_selected
               "When" is the newest history row that moved the application
               into its current status, else reviewed_at, else today. Types
               are read as step 1 leaves them, so a scholarship with a trial
               day is judged as the open_trial it becomes. Every change writes
               a history row with note "status split migration".
  3. COUNTERS  confirmed_count / selected_count recomputed from the
               applications table (nothing wrote them before 0011).
  4. AGE FLAG  age_mismatch_at_apply backfilled with the helper apply() uses,
               eligibility_service.is_age_mismatch, against the profile
               birthdate as it is today (the one at apply time is not kept).

Then it prints what it did — or, with --dry-run, would do — and a read-only
health report.

Characteristics:
  * Idempotent  — a second run changes nothing. An application with a
                  "status split migration" history row is never touched again,
                  a recruitment with a current type is not one this command
                  looks at, the benefit is only created when absent, and the
                  counter / flag passes only write rows whose value differs.
                  Safe to re-run any time.
  * Batched     — keyset pages of --batch-size rows, each written in one
                  transaction with its rows locked, so an org changing
                  statuses mid-run is neither overwritten nor double-counted.
  * Flat memory — rows are read one batch at a time.
  * --dry-run   — reads only and writes nothing, then prints the same report.

Run once after deploying migration recruitments/0011:

    python manage.py migrate_recruitment_v3 --dry-run          # report only
    python manage.py migrate_recruitment_v3
    python manage.py migrate_recruitment_v3 --batch-size 100   # smaller batches
"""

from collections import Counter

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Count, Max, Q
from django.utils import timezone

from apps.accounts.models import User
from apps.recruitments.models import (
    Recruitment,
    RecruitmentApplication,
    RecruitmentApplicationStatusHistory,
    RecruitmentBenefit,
)
from apps.recruitments.legacy_status import is_before_trial, local_date
from apps.recruitments.services.eligibility_service import is_age_mismatch

Status = RecruitmentApplication.Status
Type = Recruitment.Type

MIGRATION_NOTE = "status split migration"

# HISTORICAL VALUES, AS PLAIN STRINGS. `scholarship`, `direct_recruitment`,
# `invited` and `rejected` are no longer in Recruitment.Type or
# RecruitmentApplication.Status — they were removed once this command had run
# everywhere — so there is no enum member to name them by.
#
# That is deliberate and it is exactly why this command still exists: its
# whole job is reading rows written BEFORE they were removed. A database that
# has not been migrated yet still holds them, and a literal is the only
# honest way to say so.
LEGACY_SCHOLARSHIP = "scholarship"
LEGACY_DIRECT_RECRUITMENT = "direct_recruitment"
LEGACY_INVITED = "invited"
LEGACY_REJECTED = "rejected"

# Types this command retires.
LEGACY_TYPES = (LEGACY_SCHOLARSHIP, LEGACY_DIRECT_RECRUITMENT)

# The only statuses rewritten. Every other status is left exactly as it is.
SOURCE_STATUSES = (LEGACY_INVITED, Status.SELECTED, LEGACY_REJECTED)

# Each recruitment counter and the status it counts — the same pairing
# ApplicationService keeps in step on every status change.
COUNTED = {
    Status.TRIAL_CONFIRMED: "confirmed_count",
    Status.SELECTED: "selected_count",
}

SCHOLARSHIP_BENEFIT = {"title": "Scholarship", "icon_name": "scholarship"}

STATUS_BUCKETS = (
    ("invited_confirmed", "invited -> trial_confirmed"),
    ("selected_pre_trial", "selected -> trial_confirmed (set pre-trial)"),
    ("selected_kept", "selected -> selected"),
    ("rejected_pre_trial", "rejected -> not_shortlisted (set pre-trial)"),
    ("rejected_not_selected", "rejected -> not_selected"),
    ("unchanged", "unchanged"),
    ("already_migrated", "already migrated by an earlier run"),
)


def effective_type(recruitment_type, event_date):
    """The type a recruitment has once this command has run."""
    if recruitment_type in LEGACY_TYPES:
        return Type.OPEN_TRIAL if event_date else Type.PLAYER_LOOKING
    return recruitment_type


def resolve_status(status, recruitment_type, event_date, set_at, now, tzinfo):
    """
    (new_status, bucket) for one application in a SOURCE status.

    ``tzinfo`` is the RECRUITMENT'S zone and every date below is read in it,
    so "before the trial" means before it on the club's own calendar.

    ``set_at`` is when the org set the current status, or None when nothing
    records it — then ``now`` stands in (read in the same zone), so a trial
    day already reached counts as "set after".
    """
    if status == LEGACY_INVITED:
        return Status.TRIAL_CONFIRMED, "invited_confirmed"

    # THE SHARED RULE. `legacy_status.is_before_trial` is the one
    # implementation of "was this decided before the trial began", and the
    # live status endpoint calls the very same function for a stale client's
    # `rejected`. Two copies would eventually classify the same application
    # two different ways — the backfill one way, an unreloaded phone another.
    before_trial = is_before_trial(
        recruitment_type=effective_type(recruitment_type, event_date),
        trial_starts_at=event_date,
        decided_on=local_date(set_at or now, tzinfo),
        tzinfo=tzinfo,
    )

    if status == Status.SELECTED:
        if before_trial:
            return Status.TRIAL_CONFIRMED, "selected_pre_trial"
        return Status.SELECTED, "selected_kept"

    if before_trial:
        return Status.NOT_SHORTLISTED, "rejected_pre_trial"
    return Status.NOT_SELECTED, "rejected_not_selected"


def _id_batches(queryset, batch_size):
    """
    Primary keys of ``queryset`` in id order, one page at a time.

    Keyset pages (id > last seen), not OFFSET: a row rewritten by the previous
    page can drop out of the filter without shifting the next one.
    """
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
        "Convert existing recruitments and applications to the v3 statuses "
        "and types, recompute counters, and backfill age_mismatch_at_apply"
    )

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
            help="Rows processed per transaction (default 500).",
        )

    def handle(self, *args, **options):
        self.dry_run = options["dry_run"]
        self.batch_size = max(1, options["batch_size"])
        # The INSTANT, not a local date. Every row turns it into its own
        # venue's calendar day — there is no one "today" to precompute once
        # a recruitment carries its own timezone.
        self.now = timezone.now()

        self.type_counts = Counter()
        self.benefits_created = 0
        self.status_counts = Counter()
        # recruitment_id → {counter field: delta} for every status change. A
        # dry run writes none of them, so the counter pass adds these on top
        # of the table to report what a real run would leave behind.
        self.counter_deltas = {}
        self.counters_corrected = 0
        self.age_flags = Counter()

        self.stdout.write(self.style.WARNING(
            f"Migrating recruitments to v3{' (dry-run)' if self.dry_run else ''}..."
        ))

        self._migrate_types()
        self._migrate_statuses()
        self._recompute_counters()
        self._backfill_age_mismatch()

        self._report()

    # ── 1. types ─────────────────────────────────────────────────────

    def _migrate_types(self):
        queryset = Recruitment.objects.filter(recruitment_type__in=LEGACY_TYPES)
        processed = 0

        for ids in _id_batches(queryset, self.batch_size):
            with transaction.atomic():
                rows = queryset.filter(id__in=ids).only(
                    "id", "recruitment_type", "event_date"
                )
                if not self.dry_run:
                    rows = rows.select_for_update()
                rows = list(rows)

                benefits = self._scholarship_benefits(rows)
                for recruitment in rows:
                    new_type = effective_type(
                        recruitment.recruitment_type, recruitment.event_date
                    )
                    self.type_counts[(recruitment.recruitment_type, new_type)] += 1
                    recruitment.recruitment_type = new_type

                self.benefits_created += len(benefits)
                if not self.dry_run:
                    Recruitment.objects.bulk_update(rows, ["recruitment_type"])
                    RecruitmentBenefit.objects.bulk_create(benefits)

            processed += len(ids)
            self.stdout.write(f"  types: processed {processed}")

    def _scholarship_benefits(self, rows):
        """Unsaved "Scholarship" benefits for the scholarships lacking one."""
        scholarship_ids = [
            row.id for row in rows
            if row.recruitment_type == LEGACY_SCHOLARSHIP
        ]
        if not scholarship_ids:
            return []

        existing = RecruitmentBenefit.objects.filter(
            recruitment_id__in=scholarship_ids
        )
        # Case-insensitive: an org that typed "scholarship" already says so.
        has_benefit = set(
            existing
            .filter(title__iexact=SCHOLARSHIP_BENEFIT["title"])
            .values_list("recruitment_id", flat=True)
        )
        top_order = dict(
            existing
            .values("recruitment_id")
            .annotate(top=Max("display_order"))
            .order_by()
            .values_list("recruitment_id", "top")
        )

        return [
            RecruitmentBenefit(
                recruitment_id=recruitment_id,
                display_order=top_order.get(recruitment_id, -1) + 1,
                **SCHOLARSHIP_BENEFIT,
            )
            for recruitment_id in scholarship_ids
            if recruitment_id not in has_benefit
        ]

    # ── 2. statuses ──────────────────────────────────────────────────

    def _migrate_statuses(self):
        # Counted before anything moves, so dry run and real run agree.
        self.status_counts["unchanged"] = (
            RecruitmentApplication.objects
            .exclude(status__in=SOURCE_STATUSES)
            .count()
        )

        queryset = RecruitmentApplication.objects.filter(
            status__in=SOURCE_STATUSES
        )
        processed = 0

        for ids in _id_batches(queryset, self.batch_size):
            with transaction.atomic():
                self._migrate_status_batch(queryset, ids)
            processed += len(ids)
            self.stdout.write(f"  statuses: processed {processed}")

    def _migrate_status_batch(self, queryset, ids):
        # Re-filtered on status under the lock: a row an org moved since the
        # page was read simply drops out.
        rows = queryset.filter(id__in=ids).select_related("recruitment")
        if not self.dry_run:
            # Lock the applications only; the recruitment is just read.
            rows = rows.select_for_update(of=("self",))
        applications = list(rows)

        history = RecruitmentApplicationStatusHistory.objects.filter(
            application_id__in=ids
        )
        already_migrated = set(
            history
            .filter(note=MIGRATION_NOTE)
            .values_list("application_id", flat=True)
        )
        # The LATEST time each application entered each status — for its
        # current status, that is when the org set it.
        entered_at = {
            (row["application_id"], row["to_status"]): row["at"]
            for row in (
                history
                .filter(to_status__in=SOURCE_STATUSES)
                .values("application_id", "to_status")
                .annotate(at=Max("created_at"))
                .order_by()
            )
        }

        changed = []
        new_history = []
        for application in applications:
            if application.id in already_migrated:
                self.status_counts["already_migrated"] += 1
                continue

            recruitment = application.recruitment
            new_status, bucket = resolve_status(
                application.status,
                recruitment.recruitment_type,
                recruitment.event_date,
                entered_at.get((application.id, application.status))
                or application.reviewed_at,
                self.now,
                recruitment.zoneinfo,
            )
            self.status_counts[bucket] += 1
            if new_status == application.status:
                continue

            deltas = self.counter_deltas.setdefault(recruitment.id, Counter())
            for status, delta in ((application.status, -1), (new_status, 1)):
                field = COUNTED.get(status)
                if field:
                    deltas[field] += delta

            new_history.append(
                RecruitmentApplicationStatusHistory(
                    application=application,
                    from_status=application.status,
                    to_status=new_status,
                    changed_by=None,
                    note=MIGRATION_NOTE,
                )
            )
            application.status = new_status
            changed.append(application)

        if changed and not self.dry_run:
            RecruitmentApplication.objects.bulk_update(changed, ["status"])
            RecruitmentApplicationStatusHistory.objects.bulk_create(new_history)

    # ── 3. counters ──────────────────────────────────────────────────

    def _recompute_counters(self):
        fields = list(COUNTED.values())
        processed = 0

        for ids in _id_batches(Recruitment.objects.all(), self.batch_size):
            with transaction.atomic():
                rows = Recruitment.objects.filter(id__in=ids).only("id", *fields)
                if not self.dry_run:
                    # Locked BEFORE counting: change_status moves these same
                    # counters with F(), and it must land either wholly before
                    # the count or wholly on top of the value written here.
                    rows = rows.select_for_update()
                rows = list(rows)

                actual = Counter()
                for row in (
                    RecruitmentApplication.objects
                    .filter(recruitment_id__in=ids, status__in=list(COUNTED))
                    .values("recruitment_id", "status")
                    .annotate(n=Count("id"))
                    .order_by()
                ):
                    actual[(row["recruitment_id"], COUNTED[row["status"]])] = row["n"]

                changed = []
                for recruitment in rows:
                    pending = (
                        self.counter_deltas.get(recruitment.id, {})
                        if self.dry_run else {}
                    )
                    values = {
                        field: actual[(recruitment.id, field)] + pending.get(field, 0)
                        for field in fields
                    }
                    if any(getattr(recruitment, f) != v for f, v in values.items()):
                        for field, value in values.items():
                            setattr(recruitment, field, value)
                        changed.append(recruitment)

                self.counters_corrected += len(changed)
                if changed and not self.dry_run:
                    Recruitment.objects.bulk_update(changed, fields)

            processed += len(ids)
            self.stdout.write(f"  counters: processed {processed}")

    # ── 4. age_mismatch_at_apply ─────────────────────────────────────

    def _backfill_age_mismatch(self):
        # Only a row with a group can be a mismatch, and only a row already
        # flagged can need clearing — no other row can change.
        queryset = RecruitmentApplication.objects.filter(
            Q(age_category__isnull=False) | Q(age_mismatch_at_apply=True)
        )
        processed = 0

        for ids in _id_batches(queryset, self.batch_size):
            with transaction.atomic():
                rows = RecruitmentApplication.objects.filter(
                    id__in=ids
                ).select_related("age_category", "applicant__profile")
                if not self.dry_run:
                    rows = rows.select_for_update(of=("self",))

                changed = []
                for application in rows:
                    profile = getattr(application.applicant, "profile", None)
                    birthdate = getattr(profile, "birthdate", None)
                    flag = is_age_mismatch(
                        application.age_category,
                        birthdate.year if birthdate else None,
                    )
                    if flag != application.age_mismatch_at_apply:
                        application.age_mismatch_at_apply = flag
                        changed.append(application)
                        self.age_flags["set" if flag else "cleared"] += 1

                if changed and not self.dry_run:
                    RecruitmentApplication.objects.bulk_update(
                        changed, ["age_mismatch_at_apply"]
                    )

            processed += len(ids)
            self.stdout.write(f"  age flags: processed {processed}")

    # ── report ───────────────────────────────────────────────────────

    def _line(self, label, value, width=48):
        self.stdout.write(f"  {label:<{width}}: {value}")

    def _report(self):
        verb = "would change — dry-run, nothing written" if self.dry_run else "changed"
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING(f"STATUS BUCKETS ({verb})"))
        for key, label in STATUS_BUCKETS:
            self._line(label, self.status_counts[key])

        pre_trial = self.status_counts["selected_pre_trial"]
        self.stdout.write(self.style.WARNING(
            f"  NOTE: {pre_trial} application(s) were set to `selected` BEFORE "
            f"the trial date.\n"
            f"        That is orgs using Selected to mean 'come along' — the "
            f"exact problem\n"
            f"        this migration fixes."
        ))

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING(f"TYPE BUCKETS ({verb})"))
        for old in LEGACY_TYPES:
            for new in (Type.OPEN_TRIAL, Type.PLAYER_LOOKING):
                self._line(f"{old} -> {new}", self.type_counts[(old, new)])
        self._line("scholarship benefit rows to create", self.benefits_created)

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING(f"COUNTERS AND FLAGS ({verb})"))
        self._line(
            "recruitments with confirmed/selected recomputed",
            self.counters_corrected,
        )
        self._line("age_mismatch_at_apply set to true", self.age_flags["set"])
        self._line("age_mismatch_at_apply cleared", self.age_flags["cleared"])

        self._health_report()

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(
            "Done (dry-run — no writes)." if self.dry_run else "Done."
        ))

    def _health_report(self):
        """
        The §21 metric. Read-only, never writes, and identical whether the
        passes above wrote or not: open_trial is counted by the type a
        recruitment HAS AFTER this command (a legacy type with an event_date
        becomes one), and "passed" means the stored ``event_date`` instant
        has gone by.

        ONE DELIBERATE IMPRECISION, and it is confined to this metric. These
        two counts are QUERIES across every recruitment at once, so there is
        no per-row zone to compare against — the whole reason the real rule
        does its timezone maths at write time. ``event_date`` is the trial's
        START, so a timed trial counts as "passed" from its start time
        rather than from midnight that night. For a report that exists to
        say roughly how much history is stale, a few hours either way
        changes nothing; anything that must be exact reads
        ``trial_end_date`` through ``trial_window`` instead.
        """
        now = self.now
        live = Recruitment.objects.filter(is_deleted=False)

        total = live.count()
        active = live.filter(status=Recruitment.Status.ACTIVE).count()
        past_trials = live.filter(
            recruitment_type__in=(Type.OPEN_TRIAL, *LEGACY_TYPES),
            event_date__lt=now,
        ).count()
        stale = (
            live
            .filter(event_date__lt=now, applications__status=Status.APPLIED)
            .distinct()
            .count()
        )
        applications = (
            RecruitmentApplication.objects
            .exclude(status=Status.WITHDRAWN)
            .count()
        )
        pending = User.objects.filter(
            guardian_consent_status=User.GuardianConsentStatus.PENDING
        )
        pending_total = pending.count()
        pending_idle = pending.filter(
            recruitment_applications__isnull=True
        ).count()

        width = 62
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING(
            "HEALTH REPORT (read-only, never writes)"
        ))
        self._line(
            "recruitments total / active / open_trial with a past event_date",
            f"{total} / {active} / {past_trials}",
            width,
        )
        self.stdout.write(
            "  recruitments whose event_date has passed but still have"
        )
        self._line("  applications sitting in `applied`", stale, width)
        self._line("applications total, excluding withdrawn", applications, width)
        self._line(
            'users at guardian_consent_status = "pending"', pending_total, width
        )
        self._line(
            "  ... of whom have zero recruitment applications",
            pending_idle,
            width,
        )
