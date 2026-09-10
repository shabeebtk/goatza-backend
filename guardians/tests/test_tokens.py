"""
The link, and the three ways it stops working.

A consent token IS the parent's identity — there is no account behind it, so
whoever holds one can unlock a child. Everything here is about that credential
being narrow: it works once, it works for a week, and it stops working the
moment a newer one is minted.

The last class is the one to read twice. Every refusal on the parent's surface
has to be indistinguishable from every other, because a response that says
"expired" rather than "unknown" has told a stranger the token was real, and one
that names the child has told them whose it was.
"""

import datetime
from unittest.mock import patch

from django.utils import timezone

from accounts.models import User
from guardians.constants import TOKEN_TTL_DAYS, token_hash
from guardians.models import GuardianConsentEvent
from guardians.tests.base import (
    PARENT_NAME,
    GuardianTestCase,
    consent_url,
    make_minor,
)

CHILD_EMAIL = "tokenkid@example.com"
CHILD_USERNAME = "tokenkid"
UNKNOWN_TOKEN = "ZZZZnotarealtokenZZZZnotarealtokenZZZZ1234"


def expire(token):
    """Backdate a live link past its deadline."""
    GuardianConsentEvent.objects.filter(token_hash=token_hash(token)).update(
        token_expires_at=timezone.now() - datetime.timedelta(days=1)
    )


class TokenLifetimeTests(GuardianTestCase):

    def setUp(self):
        super().setUp()
        self.child = self.authenticate(
            make_minor(email=CHILD_EMAIL, username=CHILD_USERNAME)
        )
        _, self.token = self.ask_for_consent()

    def test_a_live_token_resolves(self):
        res = self.parent_get(consent_url(self.token))

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["data"]["state"], "pending")

    def test_the_deadline_is_the_configured_window(self):
        event = GuardianConsentEvent.objects.get(child=self.child)
        expected = timezone.now() + datetime.timedelta(days=TOKEN_TTL_DAYS)

        self.assertLess(
            abs((event.token_expires_at - expected).total_seconds()), 60
        )

    def test_an_expired_token_is_rejected(self):
        expire(self.token)

        res = self.approve_by_link(self.token)

        self.assertEqual(res.status_code, 404, res.data)
        self.assert_status(self.child, User.GuardianConsentStatus.PENDING)

    def test_an_expired_token_cannot_even_be_read(self):
        expire(self.token)

        res = self.parent_get(consent_url(self.token))

        self.assertEqual(res.status_code, 404, res.data)

    def test_the_raw_token_is_never_stored(self):
        # The database holds digests. A dump of it must not be a pile of
        # working "approve any child" links.
        event = GuardianConsentEvent.objects.get(child=self.child)

        self.assertEqual(len(event.token_hash), 64)
        self.assertNotIn(self.token, event.token_hash)
        self.assertEqual(event.token_hash, token_hash(self.token))


class OneUseOnlyTests(GuardianTestCase):

    def setUp(self):
        super().setUp()
        self.child = self.authenticate(
            make_minor(email=CHILD_EMAIL, username=CHILD_USERNAME)
        )
        _, self.token = self.ask_for_consent()

    def test_a_token_cannot_approve_twice(self):
        first = self.approve_by_link(self.token)
        second = self.approve_by_link(self.token)

        self.assertEqual(first.status_code, 200, first.data)
        self.assertEqual(second.status_code, 404, second.data)

    def test_the_second_attempt_writes_nothing(self):
        self.approve_by_link(self.token)
        self.approve_by_link(self.token)

        self.assertEqual(
            GuardianConsentEvent.objects.filter(
                child=self.child, event_type="approved"
            ).count(),
            1,
        )

    def test_a_used_token_cannot_be_declined_afterwards(self):
        self.approve_by_link(self.token)

        res = self.parent_post(f"{consent_url(self.token)}/decline")

        self.assertEqual(res.status_code, 404, res.data)
        self.assert_status(self.child, User.GuardianConsentStatus.APPROVED)

    def test_withdrawal_is_the_one_thing_a_spent_token_still_does(self):
        # The email promises "you can remove your permission later from this
        # same link". That promise outlives both the single use and the expiry.
        self.approve_by_link(self.token)
        expire(self.token)

        with patch(
            "guardians.services.consent_service"
            ".send_guardian_consent_withdrawn_email"
        ):
            res = self.parent_post(f"{consent_url(self.token)}/withdraw")

        self.assertEqual(res.status_code, 200, res.data)
        self.assert_status(self.child, User.GuardianConsentStatus.WITHDRAWN)


class ResendRetiresTheOldTokenTests(GuardianTestCase):

    def setUp(self):
        super().setUp()
        self.child = self.authenticate(
            make_minor(email=CHILD_EMAIL, username=CHILD_USERNAME)
        )
        _, self.old_token = self.ask_for_consent()

        self.resend_response, self.new_token = self.resend_consent()

    def test_the_resend_succeeds(self):
        self.assertEqual(
            self.resend_response.status_code, 200, self.resend_response.data
        )
        self.assertNotEqual(self.new_token, self.old_token)

    def test_the_old_token_stops_working(self):
        # Otherwise every resend would leave one more live credential in the
        # world for the same child.
        res = self.approve_by_link(self.old_token)

        self.assertEqual(res.status_code, 404, res.data)
        self.assert_status(self.child, User.GuardianConsentStatus.PENDING)

    def test_the_old_token_stops_rendering_a_page(self):
        res = self.parent_get(consent_url(self.old_token))

        self.assertEqual(res.status_code, 404, res.data)

    def test_the_new_token_works(self):
        res = self.approve_by_link(self.new_token)

        self.assertEqual(res.status_code, 200, res.data)
        self.assert_status(self.child, User.GuardianConsentStatus.APPROVED)

    def test_the_resent_event_carries_the_new_hash(self):
        resent = GuardianConsentEvent.objects.get(
            child=self.child, event_type="resent"
        )

        self.assertEqual(resent.token_hash, token_hash(self.new_token))


class RefusalsLeakNothingTests(GuardianTestCase):
    """
    The safety rule, checked as a rule rather than case by case.

    An anonymous caller holding a string learns exactly one thing from this
    surface: whether it works right now. Not whether it ever existed, not why it
    stopped, and never whose it was.
    """

    def setUp(self):
        super().setUp()
        self.child = self.authenticate(
            make_minor(email=CHILD_EMAIL, username=CHILD_USERNAME)
        )
        _, self.token = self.ask_for_consent()

    def _bodies(self):
        """Every refusal this surface can produce, as raw text."""
        expired_token = self.token
        expire(expired_token)

        spent_child = make_minor(email="spent@example.com", username="spentkid")
        self.authenticate(spent_child)
        _, spent_token = self.ask_for_consent(parent_email="other@example.com")
        self.approve_by_link(spent_token)

        return {
            "unknown": self.parent_get(consent_url(UNKNOWN_TOKEN)),
            "expired": self.parent_get(consent_url(expired_token)),
            "spent": self.parent_post(f"{consent_url(spent_token)}/approve", {
                "parent_name": PARENT_NAME, "confirm_18_plus": True,
            }),
        }

    def test_every_refusal_is_the_same_answer(self):
        responses = self._bodies()

        statuses = {name: res.status_code for name, res in responses.items()}
        self.assertEqual(set(statuses.values()), {404}, statuses)

        messages = {res.data["message"] for res in responses.values()}
        self.assertEqual(len(messages), 1, messages)

    def test_no_refusal_names_a_child(self):
        for name, res in self._bodies().items():
            body = str(res.data)

            self.assertNotIn(CHILD_USERNAME, body, name)
            self.assertNotIn(CHILD_EMAIL, body, name)
            self.assertNotIn("spentkid", body, name)

    def test_no_refusal_names_a_parent(self):
        for name, res in self._bodies().items():
            body = str(res.data)

            self.assertNotIn("parent@example.com", body, name)
            self.assertNotIn(PARENT_NAME, body, name)

    def test_a_refusal_carries_no_state_at_all(self):
        # No `state`, no `guardian_consent_status`, nothing a caller could
        # difference against another response to learn what happened.
        res = self.parent_get(consent_url(UNKNOWN_TOKEN))

        self.assertNotIn("state", res.data.get("data", {}))
        self.assertNotIn("child_username", res.data.get("data", {}))
