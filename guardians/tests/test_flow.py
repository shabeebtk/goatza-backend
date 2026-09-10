"""
The whole flow, end to end, through the HTTP surface.

Every test here starts at a signup and finishes with a child who is either
unlocked or deliberately not. Nothing calls the service directly — the point of
this file is the wiring: that the two signup paths lock, that the child's
endpoint and the parent's endpoint are halves of one exchange, and that the
gate's answer changes as a result.

The other files in this package go narrow on the pieces. This one is the only
place that proves they add up.
"""

from unittest.mock import patch

from accounts.models import User, UserProfile
from guardians.models import GuardianConsentEvent
from guardians.tests.base import (
    ADULT_YEARS,
    DETAILS_URL,
    FEED_URL,
    GUARDIAN_DETAILS_URL,
    MINOR_YEARS,
    PARENT_EMAIL,
    PASSWORD,
    GuardianTestCase,
    consent_url,
    make_minor,
    years_ago,
)
from legal.selectors.acceptance_selectors import get_pending_documents
from utils.cache import cache_get
from utils.cache_keys import CacheKeys

SIGNUP_URL = "/user/signup"
VERIFY_OTP_URL = "/user/verify/otp"
ROLE_URL = "/user/role"

NEW_EMAIL = "newminor@example.com"


class EmailSignupFlowTests(GuardianTestCase):
    """Signup → OTP → parent link → approve → unlocked."""

    def _signup(self, email=NEW_EMAIL, age_years=MINOR_YEARS):
        with patch("accounts.views.user_auth_views.send_signup_otp_email"):
            res = self.client.post(
                SIGNUP_URL,
                {
                    "name": "New Player",
                    "email": email,
                    "password": PASSWORD,
                    "role": User.Role.PLAYER,
                    "accepted_terms": True,
                    "birthdate": years_ago(age_years).isoformat(),
                    "country_code": "IN",
                },
                format="json",
            )

        self.assertEqual(res.status_code, 200, res.data)
        return res

    def _verify(self, email=NEW_EMAIL):
        # The OTP never leaves the cache in a test — the mail was mocked — so
        # this reads the code the signup view stored, exactly as the real
        # verification will read it.
        otp = cache_get(CacheKeys.email_otp(email))

        with patch("accounts.views.user_auth_views.send_welcome_email"):
            return self.client.post(
                VERIFY_OTP_URL, {"email": email, "otp": otp}, format="json"
            )

    def test_a_minor_finishes_signup_locked(self):
        self._signup()
        res = self._verify()

        self.assertEqual(res.status_code, 200, res.data)
        self.assertTrue(res.data["data"]["guardian_required"])

        user = User.objects.get(email=NEW_EMAIL)
        self.assertEqual(
            user.guardian_consent_status, User.GuardianConsentStatus.PENDING
        )

    def test_an_adult_finishes_signup_untouched(self):
        self._signup(email="newadult@example.com", age_years=ADULT_YEARS)
        res = self._verify("newadult@example.com")

        self.assertFalse(res.data["data"]["guardian_required"])

        user = User.objects.get(email="newadult@example.com")
        self.assertEqual(
            user.guardian_consent_status, User.GuardianConsentStatus.NOT_NEEDED
        )

    def test_the_full_email_path_ends_unlocked(self):
        self._signup()
        self._verify()
        child = User.objects.get(email=NEW_EMAIL)
        self.authenticate(child)

        # Locked: the gate refuses an ordinary read.
        self.assertEqual(self.client.get(FEED_URL).status_code, 403)

        response, token = self.ask_for_consent()
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["mode"], "link_sent")
        self.assertIsNotNone(token)

        approve = self.approve_by_link(token)
        self.assertEqual(approve.status_code, 200, approve.data)
        self.assertEqual(approve.data["data"]["result"], "approved")

        # Unlocked, on the same session that was refused above.
        self.assert_status(child, User.GuardianConsentStatus.APPROVED)
        self.assertEqual(self.client.get(FEED_URL).status_code, 200)

    def test_the_waiting_screen_can_be_rendered_while_locked(self):
        self._signup()
        self._verify()
        child = User.objects.get(email=NEW_EMAIL)
        self.authenticate(child)
        self.ask_for_consent()

        res = self.client.get(DETAILS_URL)

        self.assertEqual(res.status_code, 200, res.data)
        guardian = res.data["data"]["guardian"]
        self.assertEqual(guardian["status"], User.GuardianConsentStatus.PENDING)
        # Masked, not the address they typed.
        self.assertIn("*", guardian["masked_contact"])
        self.assertNotEqual(guardian["masked_contact"], PARENT_EMAIL)


class GoogleSignupFlowTests(GuardianTestCase):
    """
    The other half of the wiring: an account created by the Google callback,
    which asks nothing at signup and closes every gap at the role step.
    """

    def setUp(self):
        super().setUp()
        # What GoogleAuthCallbackView leaves behind: verified email, no role
        # confirmation, no birthdate, no legal country, no consent.
        self.user = User.objects.create_user(
            email="google@example.com", password=PASSWORD
        )
        self.user.is_role_confirmed = False
        self.user.is_email_verified = True
        self.user.save(update_fields=["is_role_confirmed", "is_email_verified"])
        UserProfile.objects.create(user=self.user, name="Google Player")
        self.authenticate(self.user)

    def _set_role(self, age_years=MINOR_YEARS):
        return self.client.post(
            ROLE_URL,
            {
                "role": User.Role.PLAYER,
                "accepted_terms": True,
                "birthdate": years_ago(age_years).isoformat(),
                "country_code": "IN",
            },
            format="json",
        )

    def test_the_role_step_locks_a_minor(self):
        res = self._set_role()

        self.assertEqual(res.status_code, 200, res.data)
        self.assertTrue(res.data["data"]["guardian_required"])
        self.assert_status(self.user, User.GuardianConsentStatus.PENDING)

    def test_the_role_step_leaves_an_adult_alone(self):
        res = self._set_role(age_years=ADULT_YEARS)

        self.assertFalse(res.data["data"]["guardian_required"])
        self.assert_status(self.user, User.GuardianConsentStatus.NOT_NEEDED)
        # And the account is usable immediately.
        self.assertEqual(self.client.get(FEED_URL).status_code, 200)

    def test_the_full_google_path_ends_unlocked(self):
        self._set_role()
        self.assertEqual(get_pending_documents(self.user), [])

        _, token = self.ask_for_consent()
        self.approve_by_link(token)

        self.assert_status(self.user, User.GuardianConsentStatus.APPROVED)
        self.assertEqual(self.client.get(FEED_URL).status_code, 200)


class DeclineFlowTests(GuardianTestCase):
    """
    A decline is the end of a REQUEST, never the end of an account.

    The child most often got the address wrong or named the parent who was
    never going to answer, so the only humane outcome is that they stay pending
    and can try somebody else.
    """

    def setUp(self):
        super().setUp()
        self.child = self.authenticate(make_minor())
        _, self.token = self.ask_for_consent()

    def test_declining_keeps_the_child_pending(self):
        res = self.parent_post(f"{consent_url(self.token)}/decline")

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["data"]["result"], "declined")
        self.assert_status(self.child, User.GuardianConsentStatus.PENDING)

    def test_the_child_is_still_locked_out_of_the_product(self):
        self.parent_post(f"{consent_url(self.token)}/decline")

        self.assertEqual(self.client.get(FEED_URL).status_code, 403)

    def test_a_second_request_to_a_different_parent_works(self):
        self.parent_post(f"{consent_url(self.token)}/decline")

        response, second_token = self.ask_for_consent(
            parent_email="other-parent@example.com", parent_name="Anil Nair"
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertIsNotNone(second_token)
        self.assertNotEqual(second_token, self.token)

        approve = self.approve_by_link(second_token)

        self.assertEqual(approve.status_code, 200, approve.data)
        self.assert_status(self.child, User.GuardianConsentStatus.APPROVED)

    def test_the_declined_link_is_spent(self):
        self.parent_post(f"{consent_url(self.token)}/decline")

        res = self.approve_by_link(self.token)

        self.assertEqual(res.status_code, 404, res.data)
        self.assert_status(self.child, User.GuardianConsentStatus.PENDING)


class WithdrawFlowTests(GuardianTestCase):

    def setUp(self):
        super().setUp()
        self.child = self.authenticate(make_minor())
        _, self.token = self.ask_for_consent()
        self.approve_by_link(self.token)

    def test_withdrawing_relocks_the_child(self):
        with patch(
            "guardians.services.consent_service"
            ".send_guardian_consent_withdrawn_email"
        ):
            res = self.parent_post(f"{consent_url(self.token)}/withdraw")

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["data"]["result"], "withdrawn")
        self.assert_status(self.child, User.GuardianConsentStatus.WITHDRAWN)
        self.assertEqual(self.client.get(FEED_URL).status_code, 403)

    def test_withdrawn_is_not_pending(self):
        # The two states are kept apart so nothing chases a parent who has
        # already answered. If they ever collapse, a withdrawn family starts
        # receiving reminder mail.
        with patch(
            "guardians.services.consent_service"
            ".send_guardian_consent_withdrawn_email"
        ):
            self.parent_post(f"{consent_url(self.token)}/withdraw")

        res = self.client.get(DETAILS_URL)

        self.assertEqual(
            res.data["data"]["guardian"]["status"],
            User.GuardianConsentStatus.WITHDRAWN,
        )

    def test_the_approval_row_survives_the_withdrawal(self):
        # Append-only: "approved on the 3rd, withdrawn on the 9th" is the true
        # story, and the child WAS consented for that period.
        with patch(
            "guardians.services.consent_service"
            ".send_guardian_consent_withdrawn_email"
        ):
            self.parent_post(f"{consent_url(self.token)}/withdraw")

        types = list(
            GuardianConsentEvent.objects
            .filter(child=self.child)
            .order_by("created_at")
            .values_list("event_type", flat=True)
        )
        self.assertEqual(types, ["requested", "approved", "withdrawn"])

    def test_the_child_can_ask_again_after_a_withdrawal(self):
        with patch(
            "guardians.services.consent_service"
            ".send_guardian_consent_withdrawn_email"
        ):
            self.parent_post(f"{consent_url(self.token)}/withdraw")

        response, new_token = self.ask_for_consent()
        self.approve_by_link(new_token)

        self.assert_status(self.child, User.GuardianConsentStatus.APPROVED)
