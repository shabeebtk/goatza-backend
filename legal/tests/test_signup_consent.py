"""
Consent at the two ways an account can be created.

The email form and the Google button reach the same place by different routes,
and the difference is the whole point of this file:

  * EMAIL — the form asks, so the account is created WITH consent on file, in
    the same transaction. Covered in test_legal_api.SignupConsentTests; the
    tests here are the Google half.

  * GOOGLE — nothing has asked the user anything when the account row appears.
    They pressed a button on Google's screen. So the account is created PENDING
    and the role step, which a new Google user cannot skip, is where they are
    asked and where the agreement is filed.

The rule underneath both: an acceptance is recorded only where a human was
actually shown the sentence they are agreeing to.
"""

from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import User, UserProfile
from legal.constants import TERMS_VERSION
from legal.models import LegalAcceptance
from legal.selectors.acceptance_selectors import get_pending_documents
from legal.testing import accept_current_terms

ROLE_URL = "/user/role"


def make_google_user(email="googler@example.com"):
    """
    What accounts/views/user_google_auth_views.py leaves behind: an active
    account, no role confirmed, and nothing accepted.
    """
    user = User.objects.create_user(email=email, password="unusable")
    UserProfile.objects.create(user=user, name="Googler")
    user.is_role_confirmed = False
    user.save(update_fields=["is_role_confirmed", "updated_at"])
    return user


class GoogleAccountStartsPendingTests(TestCase):

    def setUp(self):
        cache.clear()
        self.user = make_google_user()

    def test_a_new_google_account_has_accepted_nothing(self):
        # The callback must NOT file consent on the user's behalf. If this ever
        # goes green because something auto-accepted, the checkbox on the role
        # step becomes decoration over an agreement already recorded.
        self.assertFalse(LegalAcceptance.objects.filter(user=self.user).exists())
        self.assertEqual(get_pending_documents(self.user), ["terms", "privacy"])


class RoleStepConsentTests(TestCase):

    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.user = make_google_user()
        self.client.force_authenticate(user=self.user)

    def test_the_role_step_is_reachable_while_pending(self):
        # /user/role is on the gate's exempt list. If it were not, a Google
        # user would be blocked from the only step that can unblock them.
        res = self.client.post(
            ROLE_URL, {"role": "coach", "accepted_terms": True}, format="json"
        )

        self.assertEqual(res.status_code, 200, res.data)

    def test_accepting_at_the_role_step_records_both_documents(self):
        self.client.post(
            ROLE_URL, {"role": "coach", "accepted_terms": True}, format="json"
        )

        self.assertEqual(
            set(
                LegalAcceptance.objects
                .filter(user=self.user)
                .values_list("document", flat=True)
            ),
            {"terms", "privacy"},
        )

        self.user.refresh_from_db()
        self.assertEqual(self.user.terms_version, TERMS_VERSION)
        self.assertEqual(get_pending_documents(self.user), [])
        self.assertEqual(self.user.role, "coach")

    def test_the_role_is_not_set_without_consent(self):
        res = self.client.post(ROLE_URL, {"role": "coach"}, format="json")

        self.assertEqual(res.status_code, 400, res.data)

        # Neither half happened. A role saved beside a refused consent would
        # leave the account looking onboarded while still gated.
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_role_confirmed)
        self.assertNotEqual(self.user.role, "coach")
        self.assertFalse(LegalAcceptance.objects.filter(user=self.user).exists())

    def test_a_falsy_consent_flag_is_refused(self):
        for value in (False, "", 0, "false", "yes"):
            res = self.client.post(
                ROLE_URL,
                {"role": "coach", "accepted_terms": value},
                format="json",
            )
            self.assertEqual(res.status_code, 400, f"accepted_terms={value!r}")

        self.assertFalse(LegalAcceptance.objects.filter(user=self.user).exists())

    def test_the_account_becomes_usable_once_consent_is_given(self):
        # The end-to-end claim: pending Google account → role step → writes work.
        blocked = self.client.post(
            "/sports/user/sport/add", {"sport": "football"}, format="json"
        )
        self.assertEqual(blocked.status_code, 403)

        self.client.post(
            ROLE_URL, {"role": "player", "accepted_terms": True}, format="json"
        )

        after = self.client.post(
            "/sports/user/sport/add", {"sport": "football"}, format="json"
        )
        self.assertNotEqual(after.status_code, 403)


class ExistingUserRoleChangeTests(TestCase):
    """A user who already accepted must not be asked again to change role."""

    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.user = User.objects.create_user(
            email="player@example.com", password="password123"
        )
        UserProfile.objects.create(user=self.user, name="Player")
        accept_current_terms(self.user)
        self.client.force_authenticate(user=self.user)

    def test_changing_role_needs_no_consent_flag(self):
        # Nothing pending, so the requirement does not apply — an email signup
        # mid-onboarding never sees the checkbox and sends nothing.
        res = self.client.post(ROLE_URL, {"role": "scout"}, format="json")

        self.assertEqual(res.status_code, 200, res.data)
        self.user.refresh_from_db()
        self.assertEqual(self.user.role, "scout")

    def test_it_does_not_write_a_second_acceptance(self):
        before = LegalAcceptance.objects.filter(user=self.user).count()

        self.client.post(
            ROLE_URL, {"role": "scout", "accepted_terms": True}, format="json"
        )

        self.assertEqual(
            LegalAcceptance.objects.filter(user=self.user).count(), before
        )


class SignupPayloadTests(TestCase):
    """The email form's flag, at the boundary."""

    def setUp(self):
        cache.clear()
        self.client = APIClient()

    @patch("accounts.views.user_auth_views.send_signup_otp_email")
    def test_signup_requires_the_flag_before_creating_anything(self, mock_email):
        res = self.client.post(
            "/user/signup",
            {
                "name": "New",
                "email": "new@example.com",
                "password": "password123",
                "role": "player",
            },
            format="json",
        )

        self.assertEqual(res.status_code, 400)
        self.assertFalse(User.objects.filter(email="new@example.com").exists())
        mock_email.assert_not_called()
