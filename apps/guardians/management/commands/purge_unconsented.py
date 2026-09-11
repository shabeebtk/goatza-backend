"""
The end of the line for a minor nobody's parent ever approved.

``ensure_pending_for_minor`` locks the account at signup and
``HasGuardianConsentIfMinor`` keeps it locked. Nothing in the product unlocks it
except a guardian saying yes, so without this command a child whose parent never
answered would sit in a permanent 403 forever — an account that cannot be used,
cannot be unlocked, and whose data we have no consent to hold. After
``GUARDIAN_CONSENT_EXPIRY_DAYS`` (30) the honest thing is to stop holding it.

Schedule it DAILY on Render as a cron job, beside the other purge:

    python manage.py purge_unconsented

Run it by hand to see what it would do, or with a shorter window:

    python manage.py purge_unconsented --dry-run
    GUARDIAN_PURGE_AFTER_DAYS=0 python manage.py purge_unconsented

WHAT IT SELECTS, and the one thing it deliberately does not

A user is swept when their status is ``pending`` AND their NEWEST open request
(``requested`` or ``resent``) is older than the window. Newest, not oldest: a
resend restarts the clock, because a child who asked their parent again three
days ago is not an abandoned account.

A pending minor with NO request at all — somebody who finished signup and never
opened the parent form — is NOT swept, because there is no request to expire.
That is the rule as specified and it leaves those accounts locked indefinitely;
if they should age out from ``User.created_at`` instead, that is a policy
decision and belongs in the selection below, deliberately.

WHAT HAPPENS TO THE ACCOUNT

Exactly what happens to any deleted account: the SAME ``_purge`` the account
deletion job runs, imported rather than copied. That method is 200 lines of
counter arithmetic, soft deletes and anonymization, and a second copy of it here
would drift the first time somebody fixed a bug in one — silently, in the
direction of data left behind. Two extra things happen here that do not happen
there, because that job's accounts arrive already deleted and these do not:
outstanding refresh tokens are blacklisted, and the row is deactivated and
stamped, so a swept account looks exactly like one its owner deleted.

THE EXPIRED EVENT IS WRITTEN FIRST, and it is the record of why the account
went. ``GuardianConsentEvent`` is append-only and stays so — the events for a
swept child survive, pointing at the anonymized shell, exactly as their legal
acceptances do (see the KEEP bucket in purge_deleted_accounts). A guardian who
is asked in three years whether we ever contacted them has an answer.

IDEMPOTENT. A purged row is recognised by its tombstone email and skipped, so a
second run the same day (or a retried cron) changes nothing and reports zero.
"""

import logging
import os
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Max
from django.utils import timezone
from rest_framework_simplejwt.token_blacklist.models import (
    BlacklistedToken,
    OutstandingToken,
)

# The SAME deletion path, not a copy of it. See the module docstring.
from apps.accounts.management.commands.purge_deleted_accounts import (
    Command as AccountPurgeCommand,
    already_purged,
)
from apps.accounts.models import User
from apps.guardians.constants import (
    GUARDIAN_CONSENT_EXPIRY_DAYS,
    OPEN_EVENT_TYPES,
    GuardianConsentEventType,
)
from apps.guardians.models import Guardian, GuardianConsentEvent

logger = logging.getLogger(__name__)


def purge_after_days():
    """
    The window, from the environment, defaulting to the constant the rest of
    the app reads. Zero is legal — it sweeps everything currently pending,
    which is what the manual verification run wants.
    """
    raw = os.getenv("GUARDIAN_PURGE_AFTER_DAYS")
    if raw is None or raw == "":
        return GUARDIAN_CONSENT_EXPIRY_DAYS
    return int(raw)


def stale_pending_users(cutoff):
    """
    Pending accounts whose newest open request predates ``cutoff``.

    Two steps rather than a join with a subquery on the same table: group the
    events by child, keep the groups whose LATEST open request is old enough,
    then take those users. ``Max`` is what makes a resend restart the clock —
    filtering the events directly would sweep a child whose first request was
    old even though they asked again yesterday.
    """
    stale_child_ids = (
        GuardianConsentEvent.objects
        .filter(event_type__in=OPEN_EVENT_TYPES)
        .values("child_id")
        .annotate(latest_request=Max("created_at"))
        .filter(latest_request__lte=cutoff)
        .values_list("child_id", flat=True)
    )

    return (
        User.objects
        .filter(
            guardian_consent_status=User.GuardianConsentStatus.PENDING,
            id__in=list(stale_child_ids),
        )
        .select_related("profile")
        .order_by("created_at")
    )


class Command(BaseCommand):
    help = (
        "Delete minor accounts whose guardian never approved them, more than "
        "GUARDIAN_PURGE_AFTER_DAYS (default 30) days after the request."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="List what would be purged without writing anything.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        days = purge_after_days()
        cutoff = timezone.now() - timedelta(days=days)

        queryset = stale_pending_users(cutoff)

        self.stdout.write(self.style.WARNING(
            f"Purging unconsented minors whose request predates "
            f"{cutoff.isoformat()} (GUARDIAN_PURGE_AFTER_DAYS={days})"
            f"{' — dry run' if dry_run else ''}..."
        ))

        purged = 0
        skipped = 0
        failed = 0

        for user in queryset:
            if already_purged(user):
                skipped += 1
                continue

            if dry_run:
                self.stdout.write(
                    f"  would purge {user.id} (@{user.username}) — "
                    f"pending since {self._requested_at(user)}"
                )
                purged += 1
                continue

            try:
                self._expire_and_purge(user)
            except Exception as e:
                # One bad row must not abort the night's run. Its transaction
                # has rolled back, so the account is untouched and the next run
                # picks it up again.
                failed += 1
                logger.error(
                    f"[GUARDIAN PURGE] failed user={user.id} "
                    f"| {type(e).__name__}: {e}"
                )
                self.stdout.write(self.style.ERROR(
                    f"  FAILED {user.id}: {type(e).__name__}: {e}"
                ))
                continue

            purged += 1
            logger.info(f"[GUARDIAN PURGE] user={user.id} expired and purged")
            self.stdout.write(f"  purged {user.id}")

        orphans = self._clean_orphan_guardians(dry_run)

        summary = (
            f"Done. purged={purged}, already_purged={skipped}, "
            f"failed={failed}, orphan_guardians={orphans}"
            f"{' (dry-run — no writes)' if dry_run else ''}."
        )
        self.stdout.write(
            self.style.SUCCESS(summary) if not failed
            else self.style.WARNING(summary)
        )

    # ------------------------------------------------------------------ #
    # ONE ACCOUNT
    # ------------------------------------------------------------------ #
    @transaction.atomic
    def _expire_and_purge(self, user):
        """
        The expired event and the deletion, in one transaction. Either both
        happen or neither — an account destroyed with no event saying why is a
        row nobody can explain, and an event about an account that is still
        there would be a lie.
        """
        newest_request = self._newest_request(user)

        GuardianConsentEvent.objects.create(
            guardian=newest_request.guardian,
            child=user,
            event_type=GuardianConsentEventType.EXPIRED,
            # The version the request was made under, not today's. This row
            # closes THAT exchange, and the notice the guardian was shown is
            # the one that went unanswered.
            notice_version=newest_request.notice_version,
            # No token: the link this expires belonged to the request above,
            # and nothing about this row is answerable.
            token_hash="",
            token_expires_at=None,
        )

        # Nobody stays signed in to an account that is about to be emptied.
        # The deletion flow does this at confirm time; these accounts never
        # went through it, so it happens here.
        for token in OutstandingToken.objects.filter(user=user):
            BlacklistedToken.objects.get_or_create(token=token)

        # THE SAME PATH as accounts' own purge: counters, soft deletes, handle
        # release, anonymization. Instantiating the command is how you call it
        # — there is no service layer under it, and copying it here is the one
        # thing this command must not do.
        AccountPurgeCommand()._purge(user)

        # What the deletion flow would have set on the way in. Without these a
        # swept account is an anonymized row that still reads as active, which
        # nothing else in the app expects.
        user.is_active = False
        user.deletion_requested_at = timezone.now()
        user.save(update_fields=[
            "is_active", "deletion_requested_at", "updated_at",
        ])

    @staticmethod
    def _newest_request(user):
        """
        The open request this sweep is expiring. Guaranteed to exist — it is
        what put the user in the queryset — and it is where the guardian and
        the notice version for the expired row come from.
        """
        return (
            GuardianConsentEvent.objects
            .select_related("guardian")
            .filter(child=user, event_type__in=OPEN_EVENT_TYPES)
            .first()
        )

    def _requested_at(self, user):
        request_event = self._newest_request(user)
        return request_event.created_at.isoformat() if request_event else "?"

    # ------------------------------------------------------------------ #
    # ORPHANED PARENTS
    # ------------------------------------------------------------------ #
    def _clean_orphan_guardians(self, dry_run):
        """
        Guardians with no events left at all.

        A ``Guardian`` is somebody who never signed up for anything — a name, an
        email and a phone belonging to a person with no account — so a row with
        nothing left pointing at it is personal data we are keeping for no
        reason at all. That is the whole justification for deleting it, and it
        is why this runs on every sweep rather than only after one that purged
        something.

        USUALLY ZERO, and that is not a bug. The swept accounts above are
        anonymized rather than deleted, so their events survive and their
        guardians stay referenced. This catches the rows left behind when a
        child's User row is genuinely DELETED — by hand, in the admin, or by a
        future job — and the CASCADE takes their events with it.
        """
        orphans = Guardian.objects.filter(events__isnull=True)

        if dry_run:
            count = orphans.count()
            for guardian in orphans[:20]:
                self.stdout.write(f"  would delete orphan guardian {guardian.id}")
            return count

        deleted, _ = orphans.delete()

        if deleted:
            logger.info(f"[GUARDIAN PURGE] orphan guardians removed={deleted}")

        return deleted
