"""
The evidence table, and the cache that has to agree with it.

Two invariants, and everything here is one or the other.

APPEND-ONLY: every action adds exactly one row and edits none. A withdrawal
does not modify the approval it revokes, a decline does not modify the request
it answers, and an idempotent call adds nothing at all. The table's whole job is
to answer "who agreed to what, when, and on what basis" months later, and a row
that can be edited answers nothing.

THE CACHE MATCHES THE NEWEST ROW: ``User.guardian_consent_status`` is derived
from these events and could be rebuilt from them. ``assert_consistent`` is the
rebuild, run after every action — if the two ever disagree, the denormalized
column has become a second source of truth rather than a copy of this one.
"""

from unittest.mock import patch

from accounts.models import User
from guardians.models import GuardianConsentEvent
from guardians.tests.base import (
    GUARDIAN_SHARED_APPROVE_URL,
    PARENT_NAME,
    GuardianTestCase,
    consent_url,
    make_minor,
)

# What the newest event means for the child, as the service writes it. This is
# the rebuild rule the denormalized column claims to be a cache of.
STATUS_FOR_NEWEST_EVENT = {
    "requested": User.GuardianConsentStatus.PENDING,
    "resent": User.GuardianConsentStatus.PENDING,
    "approved": User.GuardianConsentStatus.APPROVED,
    # A decline ends the REQUEST, not the account — the child stays pending so
    # they can name a different guardian.
    "declined": User.GuardianConsentStatus.PENDING,
    "withdrawn": User.GuardianConsentStatus.WITHDRAWN,
}


class EventTestCase(GuardianTestCase):

    def setUp(self):
        super().setUp()
        self.child = self.authenticate(make_minor())

    def types(self):
        return list(
            GuardianConsentEvent.objects
            .filter(child=self.child)
            .order_by("created_at")
            .values_list("event_type", flat=True)
        )

    def assert_consistent(self):
        """The denormalized status equals what the newest event implies."""
        newest = (
            GuardianConsentEvent.objects
            .filter(child=self.child)
            .order_by("-created_at")
            .first()
        )
        self.child.refresh_from_db()

        if newest is None:
            self.assertEqual(
                self.child.guardian_consent_status,
                User.GuardianConsentStatus.PENDING,
            )
            return

        self.assertEqual(
            self.child.guardian_consent_status,
            STATUS_FOR_NEWEST_EVENT[newest.event_type],
            f"newest={newest.event_type}",
        )

    def withdraw(self, token):
        with patch(
            "guardians.services.consent_service"
            ".send_guardian_consent_withdrawn_email"
        ):
            return self.parent_post(f"{consent_url(token)}/withdraw")


class RowsPerActionTests(EventTestCase):

    def test_signup_alone_writes_nothing(self):
        self.assertEqual(self.types(), [])
        self.assert_consistent()

    def test_asking_writes_one_requested(self):
        self.ask_for_consent()

        self.assertEqual(self.types(), ["requested"])
        self.assert_consistent()

    def test_resending_writes_one_resent(self):
        self.ask_for_consent()

        self.resend_consent()

        self.assertEqual(self.types(), ["requested", "resent"])
        self.assert_consistent()

    def test_approving_writes_one_approved(self):
        _, token = self.ask_for_consent()
        self.approve_by_link(token)

        self.assertEqual(self.types(), ["requested", "approved"])
        self.assert_consistent()

    def test_declining_writes_one_declined(self):
        _, token = self.ask_for_consent()
        self.parent_post(f"{consent_url(token)}/decline")

        self.assertEqual(self.types(), ["requested", "declined"])
        self.assert_consistent()

    def test_withdrawing_writes_one_withdrawn(self):
        _, token = self.ask_for_consent()
        self.approve_by_link(token)
        self.withdraw(token)

        self.assertEqual(self.types(), ["requested", "approved", "withdrawn"])
        self.assert_consistent()

    def test_a_full_life_is_a_full_trail(self):
        # Ask, chase, refused, ask somebody else, approved, revoked, asked
        # again. Seven actions, seven rows, in order — and the status is
        # correct at every step, not just at the end.
        _, first = self.ask_for_consent()
        self.assert_consistent()

        _, resent_token = self.resend_consent()
        self.assert_consistent()

        self.parent_post(f"{consent_url(resent_token)}/decline")
        self.assert_consistent()

        _, second = self.ask_for_consent(parent_email="dad@example.com")
        self.assert_consistent()

        self.approve_by_link(second)
        self.assert_consistent()

        self.withdraw(second)
        self.assert_consistent()

        _, third = self.ask_for_consent()
        self.assert_consistent()

        self.assertEqual(self.types(), [
            "requested", "resent", "declined",
            "requested", "approved", "withdrawn", "requested",
        ])


class NothingIsEverEditedTests(EventTestCase):

    def test_an_approval_is_untouched_by_the_withdrawal(self):
        _, token = self.ask_for_consent()
        self.approve_by_link(token)

        approval = GuardianConsentEvent.objects.get(
            child=self.child, event_type="approved"
        )
        before = (approval.id, approval.created_at, approval.method)

        self.withdraw(token)

        approval.refresh_from_db()
        self.assertEqual(
            (approval.id, approval.created_at, approval.method), before
        )

    def test_a_request_is_untouched_by_the_answer(self):
        _, token = self.ask_for_consent()

        request_event = GuardianConsentEvent.objects.get(
            child=self.child, event_type="requested"
        )
        before = (request_event.token_hash, request_event.token_expires_at)

        self.approve_by_link(token)

        request_event.refresh_from_db()
        self.assertEqual(
            (request_event.token_hash, request_event.token_expires_at), before
        )

    def test_a_refused_action_writes_nothing(self):
        _, token = self.ask_for_consent()
        self.approve_by_link(token)

        # Spent link: refused, and the table must not grow because of it.
        self.approve_by_link(token)
        self.parent_post(f"{consent_url(token)}/decline")

        self.assertEqual(self.types(), ["requested", "approved"])
        self.assert_consistent()

    def test_an_idempotent_repeat_writes_nothing(self):
        self.ask_for_consent(parent_email=self.child.email)
        body = {"parent_name": PARENT_NAME, "confirm_18_plus": True}

        self.client.post(GUARDIAN_SHARED_APPROVE_URL, body, format="json")
        self.client.post(GUARDIAN_SHARED_APPROVE_URL, body, format="json")

        self.assertEqual(self.types(), ["requested", "approved"])
        self.assert_consistent()


class EveryRowIsSelfDescribingTests(EventTestCase):
    """
    A row has to mean something on its own, years later, to somebody who was
    not here. These are the columns that make that true.
    """

    def test_every_row_carries_the_notice_version_it_was_shown_under(self):
        _, token = self.ask_for_consent()
        self.approve_by_link(token)
        self.withdraw(token)

        versions = set(
            GuardianConsentEvent.objects
            .filter(child=self.child)
            .values_list("notice_version", flat=True)
        )
        self.assertEqual(versions, {"2026-10-01"})

    def test_method_is_set_on_approvals_and_blank_everywhere_else(self):
        # "How do you know it was the parent" is a question that only arises
        # when somebody agreed to something.
        _, token = self.ask_for_consent()
        self.approve_by_link(token)
        self.withdraw(token)

        by_type = dict(
            GuardianConsentEvent.objects
            .filter(child=self.child)
            .values_list("event_type", "method")
        )

        self.assertEqual(by_type["approved"], "separate_contact")
        self.assertEqual(by_type["requested"], "")
        self.assertEqual(by_type["withdrawn"], "")

    def test_the_whole_exchange_shares_one_token_hash(self):
        # What makes a request and its answer one exchange, and what lets a
        # withdrawal find the approval it revokes months later.
        _, token = self.ask_for_consent()
        self.approve_by_link(token)
        self.withdraw(token)

        hashes = set(
            GuardianConsentEvent.objects
            .filter(child=self.child)
            .values_list("token_hash", flat=True)
        )
        self.assertEqual(len(hashes), 1)

    def test_every_row_points_at_the_same_guardian(self):
        _, token = self.ask_for_consent()
        self.approve_by_link(token)

        guardians = set(
            GuardianConsentEvent.objects
            .filter(child=self.child)
            .values_list("guardian_id", flat=True)
        )
        self.assertEqual(len(guardians), 1)
