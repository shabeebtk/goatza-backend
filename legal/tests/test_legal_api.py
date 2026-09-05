"""
The legal HTTP surface: versions, accept, consent at signup, and the block that
rides along on user/details.

``cache.clear()`` runs in every setUp because two of these endpoints are
throttled per user and DRF keeps its counters in the shared cache. Without it a
test's budget is whatever the test that ran before it left behind, and the
failure shows up as an unexplained 429 in whichever test happens to run last.
"""

from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import User, UserProfile
from legal.constants import PRIVACY_VERSION, TERMS_VERSION
from legal.models import LegalAcceptance
from legal.selectors.acceptance_selectors import get_pending_documents
from legal.services.acceptance_service import record_acceptance

VERSIONS_URL = "/legal/versions"
ACCEPT_URL = "/legal/accept"
SIGNUP_URL = "/user/signup"
DETAILS_URL = "/user/details"


def make_user(email="player@example.com"):
    user = User.objects.create_user(email=email, password="password123")
    UserProfile.objects.create(user=user, name="Player")
    return user


class LegalVersionsTests(TestCase):

    def setUp(self):
        cache.clear()
        self.client = APIClient()

    def test_versions_are_readable_without_a_token(self):
        # The signup form has to render "I accept the terms (2026-10-01)"
        # before an account exists, so this must answer an anonymous caller.
        res = self.client.get(VERSIONS_URL)

        self.assertEqual(res.status_code, 200, res.data)
        self.assertTrue(res.data["success"])

    def test_versions_covers_every_document(self):
        res = self.client.get(VERSIONS_URL)
        data = res.data["data"]

        self.assertEqual(
            set(data.keys()), {"terms", "privacy", "guidelines", "safety"}
        )
        self.assertEqual(data["terms"], TERMS_VERSION)
        self.assertEqual(data["privacy"], PRIVACY_VERSION)

    def test_versions_leaks_nothing_but_versions(self):
        # An open endpoint. Whatever is in here is published, so the payload is
        # four strings and no user data of any kind.
        res = self.client.get(VERSIONS_URL)

        for value in res.data["data"].values():
            self.assertIsInstance(value, str)


class AcceptEndpointTests(TestCase):

    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.user = make_user()
        self.client.force_authenticate(user=self.user)

    def test_accepting_records_and_clears_the_gate(self):
        res = self.client.post(
            ACCEPT_URL, {"documents": ["terms", "privacy"]}, format="json"
        )

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(
            sorted(res.data["data"]["accepted"]), ["privacy", "terms"]
        )
        self.assertEqual(res.data["data"]["pending"], [])

        self.user.refresh_from_db()
        self.assertEqual(self.user.terms_version, TERMS_VERSION)
        self.assertEqual(LegalAcceptance.objects.filter(user=self.user).count(), 2)

    def test_a_partial_acceptance_reports_what_is_still_pending(self):
        res = self.client.post(
            ACCEPT_URL, {"documents": ["terms"]}, format="json"
        )

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["data"]["accepted"], ["terms"])
        self.assertEqual(res.data["data"]["pending"], ["privacy"])

    def test_the_captured_ip_is_the_forwarded_one(self):
        # Render puts a proxy in front of the app, so REMOTE_ADDR is the proxy
        # for everybody and an audit trail built on it says nothing.
        self.client.post(
            ACCEPT_URL,
            {"documents": ["terms"]},
            format="json",
            HTTP_X_FORWARDED_FOR="203.0.113.7, 70.41.3.18",
            HTTP_USER_AGENT="Mozilla/5.0 (iPhone)",
        )

        row = LegalAcceptance.objects.get(user=self.user, document="terms")
        self.assertEqual(row.ip_address, "203.0.113.7")
        self.assertEqual(row.user_agent, "Mozilla/5.0 (iPhone)")

    def test_a_forged_forwarded_header_does_not_break_the_write(self):
        # The header is attacker-controlled and lands in an inet column. A
        # junk value must cost the acceptance nothing.
        res = self.client.post(
            ACCEPT_URL,
            {"documents": ["terms"]},
            format="json",
            HTTP_X_FORWARDED_FOR="not-an-ip-address",
        )

        self.assertEqual(res.status_code, 200, res.data)
        row = LegalAcceptance.objects.get(user=self.user, document="terms")
        self.assertIsNone(row.ip_address)

    def test_an_unknown_document_is_a_400_and_writes_nothing(self):
        res = self.client.post(
            ACCEPT_URL, {"documents": ["terms", "cookies"]}, format="json"
        )

        self.assertEqual(res.status_code, 400, res.data)
        self.assertFalse(res.data["success"])
        self.assertFalse(LegalAcceptance.objects.filter(user=self.user).exists())

    def test_a_malformed_body_is_a_400(self):
        for body in ({}, {"documents": []}, {"documents": "terms"}):
            res = self.client.post(ACCEPT_URL, body, format="json")
            self.assertEqual(res.status_code, 400, f"body={body!r}")

    def test_accepting_requires_a_token(self):
        self.client.force_authenticate(user=None)

        res = self.client.post(
            ACCEPT_URL, {"documents": ["terms"]}, format="json"
        )

        self.assertEqual(res.status_code, 401)
        self.assertFalse(LegalAcceptance.objects.exists())

    def test_repeated_accepts_are_throttled(self):
        # Re-recording is a no-op, so the budget is not protecting data — it is
        # keeping a loop out of the audit table's write path.
        statuses = [
            self.client.post(
                ACCEPT_URL, {"documents": ["terms"]}, format="json"
            ).status_code
            for _ in range(11)
        ]

        self.assertEqual(statuses[-1], 429)
        self.assertEqual(LegalAcceptance.objects.filter(user=self.user).count(), 1)


class SignupConsentTests(TestCase):
    """Consent is captured server-side, at the moment the account exists."""

    def setUp(self):
        cache.clear()
        self.client = APIClient()

    def _signup(self, **overrides):
        payload = {
            "name": "New Player",
            "email": "new@example.com",
            "password": "password123",
            "role": User.Role.PLAYER,
            "accepted_terms": True,
        }
        payload.update(overrides)
        return self.client.post(SIGNUP_URL, payload, format="json")

    @patch("accounts.views.user_auth_views.send_signup_otp_email")
    def test_signup_records_both_documents(self, _mock_email):
        res = self._signup()

        self.assertEqual(res.status_code, 200, res.data)

        user = User.objects.get(email="new@example.com")
        self.assertEqual(
            set(
                LegalAcceptance.objects
                .filter(user=user)
                .values_list("document", flat=True)
            ),
            {"terms", "privacy"},
        )
        # Nothing to accept the moment the account exists.
        self.assertEqual(get_pending_documents(user), [])
        self.assertEqual(user.terms_version, TERMS_VERSION)

    @patch("accounts.views.user_auth_views.send_signup_otp_email")
    def test_signup_captures_the_request_context(self, _mock_email):
        self.client.post(
            SIGNUP_URL,
            {
                "name": "New Player",
                "email": "new@example.com",
                "password": "password123",
                "role": User.Role.PLAYER,
                "accepted_terms": True,
            },
            format="json",
            HTTP_X_FORWARDED_FOR="203.0.113.9",
            HTTP_USER_AGENT="Mozilla/5.0 (Android)",
        )

        row = LegalAcceptance.objects.get(document="terms")
        self.assertEqual(row.ip_address, "203.0.113.9")
        self.assertEqual(row.user_agent, "Mozilla/5.0 (Android)")

    @patch("accounts.views.user_auth_views.send_signup_otp_email")
    def test_signup_without_consent_is_rejected(self, mock_email):
        res = self._signup(accepted_terms=False)

        self.assertEqual(res.status_code, 400, res.data)
        self.assertFalse(res.data["success"])
        # No account, no acceptance, no OTP mail.
        self.assertFalse(User.objects.filter(email="new@example.com").exists())
        self.assertFalse(LegalAcceptance.objects.exists())
        mock_email.assert_not_called()

    @patch("accounts.views.user_auth_views.send_signup_otp_email")
    def test_a_missing_or_falsy_consent_flag_is_rejected(self, _mock_email):
        # Strictly True. A client that forgets the field, or sends the string
        # "false" (which is truthy), must not create an account.
        for value in (None, "", 0, "false", "no"):
            payload = {
                "name": "New Player",
                "email": "new@example.com",
                "password": "password123",
                "role": User.Role.PLAYER,
            }
            if value is not None:
                payload["accepted_terms"] = value

            # Five rejected attempts is exactly SignupThrottle's minute budget,
            # and a 429 here would look like a passing rejection for the wrong
            # reason. The subject is the consent flag, not the rate limit.
            cache.clear()

            res = self.client.post(SIGNUP_URL, payload, format="json")

            self.assertEqual(res.status_code, 400, f"accepted_terms={value!r}")

        self.assertFalse(User.objects.filter(email="new@example.com").exists())

    @patch("accounts.views.user_auth_views.send_signup_otp_email")
    def test_role_is_still_validated_before_consent(self, _mock_email):
        # Order matters for the message the form shows: an unset role is the
        # earlier problem, and reporting consent first would send the user
        # looking at the wrong field.
        res = self._signup(role="admin", accepted_terms=False)

        self.assertEqual(res.data["message"], "Invalid role")


class UserDetailsLegalBlockTests(TestCase):

    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.user = make_user()
        self.client.force_authenticate(user=self.user)

    def test_details_carries_the_pending_documents(self):
        res = self.client.get(DETAILS_URL)

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(
            res.data["data"]["legal"],
            {"pending_documents": ["terms", "privacy"],
             "requires_acceptance": True},
        )

    def test_details_reports_a_cleared_user_as_not_requiring_acceptance(self):
        record_acceptance(user=self.user, documents=["terms", "privacy"])

        res = self.client.get(DETAILS_URL)

        self.assertEqual(
            res.data["data"]["legal"],
            {"pending_documents": [], "requires_acceptance": False},
        )

    def test_the_block_reports_what_the_user_actually_accepted(self):
        # Settings prints this next to each document. It is the user's OWN
        # version, which is a different question from legal/versions' "what is
        # current" — and printing the current one there would tell a user they
        # had accepted terms the re-consent modal is still asking them about.
        res = self.client.get(DETAILS_URL)
        accepted = res.data["data"]["legal"]["accepted_versions"]

        self.assertIsNone(accepted["terms"])
        self.assertIsNone(accepted["privacy"])

        record_acceptance(user=self.user, documents=["terms"])

        res = self.client.get(DETAILS_URL)
        accepted = res.data["data"]["legal"]["accepted_versions"]

        self.assertEqual(accepted["terms"], TERMS_VERSION)
        self.assertIsNone(accepted["privacy"])

    def test_the_block_is_present_on_the_full_payload_too(self):
        # The client asks for list_type=full on its own profile screen; the
        # gate must not disappear depending on which variant was requested.
        res = self.client.get(DETAILS_URL, {"list_type": "full"})

        self.assertEqual(res.status_code, 200, res.data)
        self.assertIn("legal", res.data["data"])
        self.assertTrue(res.data["data"]["legal"]["requires_acceptance"])
