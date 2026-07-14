from unittest.mock import patch

from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import User, UserProfile
from accounts.serializers.user_serializers import UserSerializer

# accounts.urls is mounted under /user/ (see core/urls.py)
SIGNUP_URL = "/user/signup"
ROLE_URL = "/user/role"


class SignupRoleTests(TestCase):
    """Role must be captured (and validated) at email/OTP signup time."""

    def setUp(self):
        self.client = APIClient()

    @patch("accounts.views.user_auth_views.send_email_async")
    def test_signup_with_each_valid_role(self, _mock_email):
        for role in User.Role.values:
            email = f"{role}@example.com"

            res = self.client.post(
                SIGNUP_URL,
                {
                    "name": role.title(),
                    "email": email,
                    "password": "password123",
                    "role": role,
                },
                format="json",
            )

            self.assertEqual(res.status_code, 200, res.data)
            self.assertTrue(res.data["success"])

            user = User.objects.get(email=email)
            self.assertEqual(user.role, role)
            # Email/OTP signups pick their role explicitly, so it's confirmed.
            self.assertTrue(user.is_role_confirmed)

    @patch("accounts.views.user_auth_views.send_email_async")
    def test_signup_missing_role_is_rejected(self, mock_email):
        res = self.client.post(
            SIGNUP_URL,
            {
                "name": "No Role",
                "email": "norole@example.com",
                "password": "password123",
            },
            format="json",
        )

        self.assertEqual(res.status_code, 400)
        self.assertFalse(res.data["success"])
        self.assertEqual(res.data["message"], "Invalid role")
        # No user should be created, and no OTP email should be sent.
        self.assertFalse(User.objects.filter(email="norole@example.com").exists())
        mock_email.assert_not_called()

    @patch("accounts.views.user_auth_views.send_email_async")
    def test_signup_invalid_role_is_rejected(self, mock_email):
        # "team"/"academy" are org TYPES, not user roles; "admin" is never valid.
        for bad_role in ["team", "academy", "admin", ""]:
            res = self.client.post(
                SIGNUP_URL,
                {
                    "name": "Bad Role",
                    "email": "badrole@example.com",
                    "password": "password123",
                    "role": bad_role,
                },
                format="json",
            )
            self.assertEqual(res.status_code, 400, f"role={bad_role!r}")
            self.assertEqual(res.data["message"], "Invalid role")

        self.assertFalse(User.objects.filter(email="badrole@example.com").exists())
        mock_email.assert_not_called()


class SetUserRoleTests(TestCase):
    """POST /user/role is the one-time onboarding step for OAuth users."""

    def setUp(self):
        self.client = APIClient()

    def _make_user(self, is_role_confirmed, role=User.Role.PLAYER, email="oauth@example.com"):
        user = User.objects.create_user(
            email=email,
            username=email.split("@")[0],
            password="password123",
            role=role,
            is_role_confirmed=is_role_confirmed,
        )
        UserProfile.objects.create(user=user, name="OAuth User")
        return user

    def test_unconfirmed_user_can_set_role(self):
        user = self._make_user(is_role_confirmed=False)
        self.client.force_authenticate(user=user)

        res = self.client.post(ROLE_URL, {"role": "coach"}, format="json")

        self.assertEqual(res.status_code, 200, res.data)
        self.assertTrue(res.data["success"])
        self.assertEqual(res.data["data"]["role"], "coach")
        self.assertTrue(res.data["data"]["is_role_confirmed"])

        user.refresh_from_db()
        self.assertEqual(user.role, "coach")
        self.assertTrue(user.is_role_confirmed)

    def test_unconfirmed_user_can_set_org_user_role(self):
        # org_user is the newly introduced role — make sure it's accepted here too.
        user = self._make_user(is_role_confirmed=False)
        self.client.force_authenticate(user=user)

        res = self.client.post(ROLE_URL, {"role": "org_user"}, format="json")

        self.assertEqual(res.status_code, 200, res.data)
        user.refresh_from_db()
        self.assertEqual(user.role, "org_user")
        self.assertTrue(user.is_role_confirmed)

    def test_confirmed_user_cannot_change_role(self):
        user = self._make_user(is_role_confirmed=True, role=User.Role.PLAYER)
        self.client.force_authenticate(user=user)

        res = self.client.post(ROLE_URL, {"role": "scout"}, format="json")

        self.assertEqual(res.status_code, 400)
        self.assertFalse(res.data["success"])
        self.assertEqual(res.data["message"], "Role already set")

        # Role must be left untouched.
        user.refresh_from_db()
        self.assertEqual(user.role, User.Role.PLAYER)
        self.assertTrue(user.is_role_confirmed)

    def test_set_role_invalid_value_is_rejected(self):
        user = self._make_user(is_role_confirmed=False)
        self.client.force_authenticate(user=user)

        res = self.client.post(ROLE_URL, {"role": "team"}, format="json")

        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["message"], "Invalid role")
        # Still unconfirmed — the invalid attempt changed nothing.
        user.refresh_from_db()
        self.assertFalse(user.is_role_confirmed)

    def test_set_role_requires_authentication(self):
        res = self.client.post(ROLE_URL, {"role": "coach"}, format="json")
        self.assertEqual(res.status_code, 401)


class UserSerializerRoleTests(TestCase):
    """The serialized user (login / OTP verify / Google) must carry role state."""

    def test_serializer_includes_role_and_confirmation_flag(self):
        user = User.objects.create_user(
            email="ser@example.com",
            username="seruser",
            password="password123",
            role=User.Role.SCOUT,
        )
        UserProfile.objects.create(user=user, name="Ser User")

        data = UserSerializer(user).data

        self.assertIn("role", data)
        self.assertIn("is_role_confirmed", data)
        self.assertEqual(data["role"], "scout")
        self.assertTrue(data["is_role_confirmed"])
