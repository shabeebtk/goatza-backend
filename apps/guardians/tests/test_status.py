"""
The ``guardian`` block on GET /user/details — what the child's waiting screen
is built from.

Two things are pinned here. The KEY NAMES, because the client reads them by
string and a renamed key does not fail, it just leaves a locked child looking
at the wrong screen. And ``request_state``, because it is what turns "still
waiting" into "your parent said no" or "the email expired" — without it the
child taps continue, bounces straight back, and has no idea why.

The rule for the extra keys is simple: real values only while ``pending`` with
a parent named. Everywhere else they are ``False`` / ``None``, never absent.
"""

import datetime

from django.utils import timezone

from apps.accounts.models import User
from apps.guardians.models import GuardianConsentEvent
from apps.guardians.selectors.consent_selectors import guardian_status
from apps.guardians.tests.base import (
    DETAILS_URL,
    GuardianTestCase,
    consent_url,
    make_adult,
    make_minor,
)

CHILD_EMAIL = "statuskid@example.com"

BLOCK_KEYS = {"status", "masked_contact", "same_as_login_contact", "request_state"}


def expire_open_links(child):
    """Backdate every live link for this child past its deadline."""
    GuardianConsentEvent.objects.filter(child=child).update(
        token_expires_at=timezone.now() - datetime.timedelta(days=1)
    )


class BlockShapeTests(GuardianTestCase):

    def _block(self):
        res = self.client.get(DETAILS_URL)
        self.assertEqual(res.status_code, 200, res.data)
        return res.data["data"]["guardian"]

    def test_the_keys_before_a_parent_is_named(self):
        self.authenticate(make_minor(email=CHILD_EMAIL, username="statuskid"))

        block = self._block()

        self.assertEqual(set(block), BLOCK_KEYS)
        self.assertEqual(block["status"], User.GuardianConsentStatus.PENDING)
        self.assertIsNone(block["masked_contact"])
        self.assertFalse(block["same_as_login_contact"])
        self.assertIsNone(block["request_state"])

    def test_the_keys_while_waiting(self):
        self.authenticate(make_minor(email=CHILD_EMAIL, username="statuskid"))
        self.ask_for_consent(parent_email="parent@example.com")

        block = self._block()

        self.assertEqual(set(block), BLOCK_KEYS)
        self.assertEqual(block["status"], User.GuardianConsentStatus.PENDING)
        self.assertIn("*", block["masked_contact"])
        self.assertFalse(block["same_as_login_contact"])
        self.assertEqual(block["request_state"], "waiting")

    def test_same_as_login_contact_is_true_for_the_childs_own_address(self):
        self.authenticate(make_minor(email=CHILD_EMAIL, username="statuskid"))
        self.ask_for_consent(parent_email=CHILD_EMAIL.upper())

        block = self._block()

        self.assertTrue(block["same_as_login_contact"])
        self.assertEqual(block["request_state"], "waiting")

    def test_an_adult_gets_the_same_keys_and_no_values(self):
        self.authenticate(make_adult())

        block = self._block()

        self.assertEqual(set(block), BLOCK_KEYS)
        self.assertEqual(block["status"], User.GuardianConsentStatus.NOT_NEEDED)
        self.assertIsNone(block["masked_contact"])
        self.assertFalse(block["same_as_login_contact"])
        self.assertIsNone(block["request_state"])

    def test_an_approved_minor_carries_no_request_state(self):
        # Approved is not a waiting state. The keys stay so the client's shape
        # never changes; the values are the empty ones.
        self.authenticate(make_minor(email=CHILD_EMAIL, username="statuskid"))
        _, token = self.ask_for_consent(parent_email=CHILD_EMAIL)
        self.approve_by_link(token)

        block = self._block()

        self.assertEqual(block["status"], User.GuardianConsentStatus.APPROVED)
        self.assertIsNone(block["masked_contact"])
        self.assertFalse(block["same_as_login_contact"])
        self.assertIsNone(block["request_state"])


class RequestStateTests(GuardianTestCase):

    def setUp(self):
        super().setUp()
        self.child = self.authenticate(
            make_minor(email=CHILD_EMAIL, username="statuskid")
        )
        _, self.token = self.ask_for_consent(parent_email="parent@example.com")

    def _state(self):
        res = self.client.get(DETAILS_URL)
        return res.data["data"]["guardian"]["request_state"]

    def test_waiting_while_the_link_is_live(self):
        self.assertEqual(self._state(), "waiting")

    def test_expired_once_the_link_has_lapsed_unanswered(self):
        expire_open_links(self.child)

        self.assertEqual(self._state(), "expired")

    def test_declined_when_the_parent_said_no(self):
        self.parent_post(f"{consent_url(self.token)}/decline")

        self.assertEqual(self._state(), "declined")
        # Still names the address the link went to, so the child knows who
        # answered.
        res = self.client.get(DETAILS_URL)
        self.assertIn("*", res.data["data"]["guardian"]["masked_contact"])

    def test_a_decline_outranks_expiry(self):
        # The declined link is spent whatever its deadline says, and "your
        # parent said no" is a different next step from "they never saw it".
        self.parent_post(f"{consent_url(self.token)}/decline")
        expire_open_links(self.child)

        self.assertEqual(self._state(), "declined")

    def test_a_resend_puts_the_child_back_to_waiting(self):
        expire_open_links(self.child)
        self.assertEqual(self._state(), "expired")

        self.resend_consent()

        self.assertEqual(self._state(), "waiting")

    def test_a_new_request_after_a_decline_is_waiting(self):
        self.parent_post(f"{consent_url(self.token)}/decline")

        self.ask_for_consent(parent_email="other-parent@example.com")

        self.assertEqual(self._state(), "waiting")


class QueryBudgetTests(GuardianTestCase):
    """
    The block rides on every session start, for every account. The adults —
    almost everybody — must cost nothing; a waiting child costs at most two
    small reads.
    """

    def test_a_non_pending_status_queries_nothing(self):
        adult = make_adult()

        with self.assertNumQueries(0):
            guardian_status(adult)

    def test_a_pending_child_with_a_live_link_costs_one_query(self):
        child = self.authenticate(
            make_minor(email=CHILD_EMAIL, username="statuskid")
        )
        self.ask_for_consent(parent_email="parent@example.com")

        with self.assertNumQueries(1):
            guardian_status(child)

    def test_a_declined_child_costs_two(self):
        child = self.authenticate(
            make_minor(email=CHILD_EMAIL, username="statuskid")
        )
        _, token = self.ask_for_consent(parent_email="parent@example.com")
        self.parent_post(f"{consent_url(token)}/decline")

        with self.assertNumQueries(2):
            guardian_status(child)
