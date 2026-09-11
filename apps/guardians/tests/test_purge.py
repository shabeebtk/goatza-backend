"""
The expiry sweep, from the outside.

The subject of every test here is a minor whose parent never answered — the
one account state the product cannot resolve on its own, and the one where
both failure directions are expensive. Sweeping too eagerly destroys a child's
account while their parent is still deciding; sweeping too late leaves data we
have no consent to hold.

Age is produced by backdating the EVENT rather than by patching the window, so
the command runs its real query against real rows. A test that set
GUARDIAN_PURGE_AFTER_DAYS=0 would prove the arithmetic and prove nothing about
whether the selection finds the right children.
"""

import datetime
from io import StringIO

from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from apps.accounts.management.commands.purge_deleted_accounts import (
    ANON_EMAIL_DOMAIN,
    ANON_NAME,
)
from apps.accounts.models import User, UserProfile
from apps.guardians.constants import GUARDIAN_CONSENT_EXPIRY_DAYS
from apps.guardians.models import Guardian, GuardianConsentEvent
from apps.guardians.services.consent_service import (
    ensure_pending_for_minor,
    request_consent,
)
from apps.legal.testing import accept_current_terms
from apps.usernames.services.username_service import UsernameService

MINOR_YEARS = 14


def years_ago(years):
    return datetime.date.today() - datetime.timedelta(days=365 * years)


def make_minor(email, username):
    """A locked minor, built the way signup builds one."""
    user = User.objects.create_user(
        email=email, password="password123", country_code="IN"
    )
    UserProfile.objects.create(
        user=user, name="Purge Test", birthdate=years_ago(MINOR_YEARS)
    )
    UsernameService.claim(username, user=user)
    accept_current_terms(user)
    ensure_pending_for_minor(user)
    user.refresh_from_db()
    return user


def ask_a_parent(user, parent_email="purge-parent@example.com"):
    """Name a guardian, exactly as POST /guardian/details does."""
    return request_consent(
        child=user, parent_name="Priya Nair", parent_contact=parent_email
    )


def age_requests(user, days):
    """
    Backdate every open request for this child by ``days``.

    An UPDATE on an append-only table, which nothing in the application is
    allowed to do — that is precisely why it lives in a test helper and not in
    a service. ``created_at`` is auto_now_add, so this is the only way to
    produce a request that is genuinely old.
    """
    GuardianConsentEvent.objects.filter(
        child=user, event_type__in=("requested", "resent")
    ).update(created_at=timezone.now() - datetime.timedelta(days=days))


def run_purge(**options):
    out = StringIO()
    call_command("purge_unconsented", stdout=out, stderr=out, **options)
    return out.getvalue()


class PurgeTestCase(TestCase):

    def setUp(self):
        cache.clear()


class TheSweepTests(PurgeTestCase):

    def setUp(self):
        super().setUp()
        self.minor = make_minor("stale@example.com", "purgestale")
        ask_a_parent(self.minor)
        age_requests(self.minor, GUARDIAN_CONSENT_EXPIRY_DAYS + 1)

    def test_the_account_is_purged(self):
        run_purge()

        self.minor.refresh_from_db()
        self.assertTrue(self.minor.email.endswith(f"@{ANON_EMAIL_DOMAIN}"))
        self.assertIsNone(self.minor.username)
        self.assertFalse(self.minor.is_active)
        self.assertIsNotNone(self.minor.deletion_requested_at)

    def test_the_profile_is_anonymized_the_same_way(self):
        # The claim that matters about reusing the other command's _purge: a
        # minor swept here comes out identical to an account its owner deleted.
        # If this drifts, the two paths have stopped being one path.
        run_purge()

        profile = UserProfile.objects.get(user=self.minor)
        self.assertEqual(profile.name, ANON_NAME)
        self.assertIsNone(profile.birthdate)
        self.assertEqual(profile.profile_photo, "")

    def test_an_expired_event_is_written_first(self):
        run_purge()

        events = list(
            GuardianConsentEvent.objects
            .filter(child=self.minor)
            .order_by("created_at")
            .values_list("event_type", flat=True)
        )
        self.assertEqual(events, ["requested", "expired"])

    def test_the_expired_event_survives_the_purge(self):
        # Append-only: the evidence that we asked outlives the account. A
        # guardian asking in three years whether we ever contacted them has an
        # answer, and it points at the anonymized shell.
        run_purge()

        expired = GuardianConsentEvent.objects.get(
            child=self.minor, event_type="expired"
        )
        self.assertIsNotNone(expired.guardian_id)
        self.assertEqual(expired.notice_version, "2026-10-01")

    def test_the_handle_goes_back_to_the_namespace(self):
        run_purge()

        self.assertTrue(UsernameService.is_available("purgestale"))

    def test_running_twice_changes_nothing(self):
        run_purge()
        output = run_purge()

        self.assertIn("purged=0", output)
        self.assertEqual(
            GuardianConsentEvent.objects.filter(
                child=self.minor, event_type="expired"
            ).count(),
            1,
        )


class SurvivorsTests(PurgeTestCase):
    """Everything the sweep must NOT touch."""

    def test_a_request_younger_than_the_window_survives(self):
        minor = make_minor("young@example.com", "purgeyoung")
        ask_a_parent(minor)
        age_requests(minor, GUARDIAN_CONSENT_EXPIRY_DAYS - 1)

        run_purge()

        minor.refresh_from_db()
        self.assertEqual(minor.email, "young@example.com")
        self.assertTrue(minor.is_active)
        self.assertFalse(
            GuardianConsentEvent.objects.filter(
                child=minor, event_type="expired"
            ).exists()
        )

    def test_a_resend_restarts_the_clock(self):
        # The reason the query takes MAX(created_at) rather than filtering the
        # events directly: this child's FIRST request is ancient, but they
        # asked again yesterday and are not an abandoned account.
        from apps.guardians.services.consent_service import resend

        minor = make_minor("resent@example.com", "purgeresent")
        ask_a_parent(minor)
        age_requests(minor, GUARDIAN_CONSENT_EXPIRY_DAYS + 10)
        resend(child=minor)

        run_purge()

        minor.refresh_from_db()
        self.assertEqual(minor.email, "resent@example.com")

    def test_an_approved_minor_is_never_swept(self):
        from apps.guardians.testing import grant_guardian_consent

        minor = make_minor("approved@example.com", "purgeapproved")
        grant_guardian_consent(minor)
        age_requests(minor, GUARDIAN_CONSENT_EXPIRY_DAYS + 10)

        run_purge()

        minor.refresh_from_db()
        self.assertEqual(minor.email, "approved@example.com")
        self.assertEqual(
            minor.guardian_consent_status, User.GuardianConsentStatus.APPROVED
        )

    def test_a_pending_minor_who_never_asked_is_left_alone(self):
        # Documented behaviour, not an accident: there is no request to expire,
        # so there is nothing for this command to act on. If these should age
        # out from User.created_at, that is a policy change in the selection.
        minor = make_minor("neverasked@example.com", "purgenever")

        run_purge()

        minor.refresh_from_db()
        self.assertEqual(minor.email, "neverasked@example.com")
        self.assertEqual(
            minor.guardian_consent_status, User.GuardianConsentStatus.PENDING
        )


class DryRunTests(PurgeTestCase):

    def setUp(self):
        super().setUp()
        self.minor = make_minor("dry@example.com", "purgedry")
        ask_a_parent(self.minor)
        age_requests(self.minor, GUARDIAN_CONSENT_EXPIRY_DAYS + 1)

    def test_it_reports_what_it_would_do(self):
        output = run_purge(dry_run=True)

        self.assertIn("would purge", output)
        self.assertIn(str(self.minor.id), output)
        self.assertIn("dry-run", output)

    def test_it_writes_nothing_at_all(self):
        run_purge(dry_run=True)

        self.minor.refresh_from_db()
        self.assertEqual(self.minor.email, "dry@example.com")
        self.assertTrue(self.minor.is_active)
        self.assertFalse(
            GuardianConsentEvent.objects.filter(
                child=self.minor, event_type="expired"
            ).exists()
        )

    def test_the_real_run_still_works_afterwards(self):
        # A dry run that consumed the row — by marking it, or by leaving a
        # half-written event — would be worse than no dry run at all.
        run_purge(dry_run=True)
        run_purge()

        self.minor.refresh_from_db()
        self.assertTrue(self.minor.email.endswith(f"@{ANON_EMAIL_DOMAIN}"))


class OrphanGuardianTests(PurgeTestCase):

    def test_a_guardian_with_no_events_is_deleted(self):
        # The state a genuinely deleted child leaves behind: GuardianConsentEvent
        # CASCADEs on the child FK, so the events go and the parent's contact
        # details are left pointing at nothing. Nobody consented to us keeping
        # those.
        minor = make_minor("orphan@example.com", "purgeorphan")
        ask_a_parent(minor, "orphan-parent@example.com")
        guardian_id = Guardian.objects.get(email="orphan-parent@example.com").id

        User.objects.filter(id=minor.id).delete()
        self.assertFalse(
            GuardianConsentEvent.objects.filter(guardian_id=guardian_id).exists()
        )

        run_purge()

        self.assertFalse(Guardian.objects.filter(id=guardian_id).exists())

    def test_a_guardian_with_events_is_kept(self):
        minor = make_minor("kept@example.com", "purgekept")
        ask_a_parent(minor, "kept-parent@example.com")
        guardian_id = Guardian.objects.get(email="kept-parent@example.com").id

        run_purge()

        self.assertTrue(Guardian.objects.filter(id=guardian_id).exists())

    def test_a_swept_childs_guardian_is_kept(self):
        # The anonymizing purge leaves the events in place, so the guardian is
        # still referenced and must survive. This is what makes the orphan
        # sweep a safety net rather than part of the deletion.
        minor = make_minor("sweptkeep@example.com", "purgesweptkeep")
        ask_a_parent(minor, "swept-parent@example.com")
        age_requests(minor, GUARDIAN_CONSENT_EXPIRY_DAYS + 1)
        guardian_id = Guardian.objects.get(email="swept-parent@example.com").id

        run_purge()

        self.assertTrue(Guardian.objects.filter(id=guardian_id).exists())

    def test_dry_run_keeps_orphans(self):
        minor = make_minor("dryorphan@example.com", "purgedryorphan")
        ask_a_parent(minor, "dry-orphan-parent@example.com")
        guardian_id = Guardian.objects.get(
            email="dry-orphan-parent@example.com"
        ).id
        User.objects.filter(id=minor.id).delete()

        output = run_purge(dry_run=True)

        self.assertIn("would delete orphan guardian", output)
        self.assertTrue(Guardian.objects.filter(id=guardian_id).exists())
