"""The OTP emails: what goes on the wire, and that nothing here can 500.

Two things are worth pinning down. First the rendered HTML — a template that
silently drops a variable still returns a 200 to the user and an unusable code
to their inbox, so the assertions are about the finished mail, not the context
dict. Second the swallow: an email is a side effect of a request, and a
template error must never surface as a failed signup.

accounts.urls is mounted under /user/ (see core/urls.py).
"""

from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings

from rest_framework.test import APIClient

from accounts.models import User, UserProfile
from legal.testing import accept_current_terms
from utils.transactional_emails import (
    send_login_otp_email,
    send_password_reset_otp_email,
    send_signup_otp_email,
)

SIGNUP_URL = "/user/signup"
LOGIN_URL = "/user/login"
FORGOT_PASSWORD_URL = "/user/forgot/password"

NAME = "Arjun"
EMAIL = "arjun@example.com"
OTP = "482913"
PASSWORD = "password123"

# (function, subject, heading, footer reason) — the subjects are contractual:
# they are what lands in the inbox list, and one of them used to name a
# different product entirely.
CASES = [
    (
        send_signup_otp_email,
        "Your Goatza verification code",
        "Verify your email",
        "this email was used to create a Goatza account",
    ),
    (
        send_login_otp_email,
        "Your Goatza login code",
        "Confirm it&rsquo;s you",
        "a login was attempted on your Goatza account",
    ),
    (
        send_password_reset_otp_email,
        "Reset your Goatza password",
        "Reset your password",
        "a password reset was requested for your Goatza account",
    ),
]


class OTPEmailRenderingTests(TestCase):
    """Every OTP email, as the recipient's client would receive it."""

    def _sent(self, send):
        """Call `send` with a mocked transport and return its send kwargs."""
        with patch("utils.transactional_emails.send_email_async") as mock_send:
            send(name=NAME, email=EMAIL, otp=OTP)

        self.assertEqual(mock_send.call_count, 1)
        return mock_send.call_args.kwargs

    def test_html_carries_the_code_the_name_and_the_brand(self):
        for send, _subject, heading, footer_reason in CASES:
            with self.subTest(email=send.__name__):
                html = self._sent(send)["html_message"]

                self.assertIn(OTP, html)
                self.assertIn(NAME, html)
                self.assertIn(heading, html)
                self.assertIn("GOATZA", html)
                self.assertIn(footer_reason, html)

    def test_html_has_no_unrendered_variables_and_no_stale_brand(self):
        for send, _subject, _heading, _footer_reason in CASES:
            with self.subTest(email=send.__name__):
                html = self._sent(send)["html_message"]

                # A missing context key renders as "" rather than raising, so
                # the tell is a literal tag that survived — a typo'd block, or
                # a template rendered without extending base.html.
                self.assertNotIn("{{", html)
                self.assertNotIn("{%", html)
                # The forgot-password subject named another product for months.
                self.assertNotIn("LearningMate", html)

    def test_subject_and_plain_text_body(self):
        for send, subject, _heading, _footer_reason in CASES:
            with self.subTest(email=send.__name__):
                kwargs = self._sent(send)

                self.assertEqual(kwargs["subject"], subject)
                self.assertEqual(kwargs["to_email"], EMAIL)

                # text/plain is not a fallback nobody reads: it is what a
                # client with HTML off shows, so the code has to be in it too.
                text = kwargs["message"]
                self.assertTrue(text.strip())
                self.assertIn(OTP, text)
                self.assertIn(NAME, text)

    def test_links_are_absolute_and_point_at_the_frontend(self):
        with override_settings(FRONTEND_BASE_URL="https://staging.goatza.com"):
            html = self._sent(send_signup_otp_email)["html_message"]

        self.assertIn("https://staging.goatza.com/privacy", html)
        self.assertIn("https://staging.goatza.com/terms", html)
        self.assertIn("https://staging.goatza.com/report-problem", html)

    def test_a_trailing_slash_in_the_setting_does_not_double_up(self):
        with override_settings(FRONTEND_BASE_URL="https://goatza.com/"):
            html = self._sent(send_signup_otp_email)["html_message"]

        self.assertNotIn("//privacy", html)
        self.assertIn("https://goatza.com/privacy", html)

    def test_the_recipient_name_is_escaped(self):
        # Copy constants are marked safe; the name never is.
        with patch("utils.transactional_emails.send_email_async") as mock_send:
            send_signup_otp_email(
                name="<script>alert(1)</script>", email=EMAIL, otp=OTP
            )

        html = mock_send.call_args.kwargs["html_message"]
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)


class OTPEmailFailureTests(TestCase):
    """A broken email must not break the request that triggered it."""

    def test_a_failing_transport_is_swallowed_and_logged(self):
        for send, _subject, _heading, _footer_reason in CASES:
            with self.subTest(email=send.__name__):
                with patch(
                    "utils.transactional_emails.send_email_async",
                    side_effect=RuntimeError("resend is down"),
                ):
                    with self.assertLogs(
                        "utils.transactional_emails", level="WARNING"
                    ) as logs:
                        send(name=NAME, email=EMAIL, otp=OTP)

                self.assertIn("send failed", logs.output[0])

    def test_a_missing_template_is_swallowed(self):
        from utils import transactional_emails

        with self.assertLogs("utils.transactional_emails", level="WARNING"):
            transactional_emails._send(
                subject="anything",
                text_body="anything",
                html_template="emails/does-not-exist.html",
                context={},
                to_email=EMAIL,
            )


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
)
class AuthViewEmailWiringTests(TestCase):
    """Each auth view still answers as before and sends exactly one mail."""

    def setUp(self):
        # The signup/login/forgot throttles all count in the shared cache, so
        # a budget already spent by another module would make these 429.
        cache.clear()
        self.client = APIClient()

    def _unverified_user(self):
        user = User.objects.create_user(
            email=EMAIL,
            username="arjun",
            password=PASSWORD,
            role=User.Role.PLAYER,
        )
        accept_current_terms(user)
        UserProfile.objects.create(user=user, name=NAME)
        return user

    @patch("accounts.views.user_auth_views.send_signup_otp_email")
    def test_signup_sends_one_signup_otp(self, mock_send):
        res = self.client.post(
            SIGNUP_URL,
            {
                "name": NAME,
                "email": "new@example.com",
                "password": PASSWORD,
                "role": User.Role.PLAYER,
                "accepted_terms": True,
            },
            format="json",
        )

        self.assertEqual(res.status_code, 200, res.data)
        self.assertTrue(res.data["success"])

        self.assertEqual(mock_send.call_count, 1)
        kwargs = mock_send.call_args.kwargs
        self.assertEqual(kwargs["name"], NAME)
        self.assertEqual(kwargs["email"], "new@example.com")
        self.assertTrue(kwargs["otp"])

    @patch("accounts.views.user_auth_views.send_login_otp_email")
    def test_login_with_an_unverified_email_sends_one_login_otp(self, mock_send):
        self._unverified_user()

        res = self.client.post(
            LOGIN_URL,
            {"email": EMAIL, "password": PASSWORD},
            format="json",
        )

        self.assertEqual(res.status_code, 200, res.data)
        self.assertTrue(res.data["data"]["verification_required"])

        self.assertEqual(mock_send.call_count, 1)
        kwargs = mock_send.call_args.kwargs
        self.assertEqual(kwargs["name"], NAME)
        self.assertEqual(kwargs["email"], EMAIL)

    @patch("accounts.views.user_auth_views.send_login_otp_email")
    def test_a_verified_login_sends_nothing(self, mock_send):
        user = self._unverified_user()
        user.is_email_verified = True
        user.save(update_fields=["is_email_verified"])

        res = self.client.post(
            LOGIN_URL,
            {"email": EMAIL, "password": PASSWORD},
            format="json",
        )

        self.assertEqual(res.status_code, 200, res.data)
        mock_send.assert_not_called()

    @patch("accounts.views.user_auth_views.send_password_reset_otp_email")
    def test_forgot_password_sends_one_reset_otp(self, mock_send):
        self._unverified_user()

        res = self.client.post(
            FORGOT_PASSWORD_URL,
            {"email": EMAIL},
            format="json",
        )

        self.assertEqual(res.status_code, 200, res.data)
        self.assertTrue(res.data["success"])

        self.assertEqual(mock_send.call_count, 1)
        kwargs = mock_send.call_args.kwargs
        self.assertEqual(kwargs["name"], NAME)
        self.assertEqual(kwargs["email"], EMAIL)
        self.assertTrue(kwargs["otp"])

    @patch("accounts.views.user_auth_views.send_password_reset_otp_email")
    def test_forgot_password_for_an_unknown_email_sends_nothing(self, mock_send):
        res = self.client.post(
            FORGOT_PASSWORD_URL,
            {"email": "nobody@example.com"},
            format="json",
        )

        self.assertEqual(res.status_code, 404)
        mock_send.assert_not_called()
