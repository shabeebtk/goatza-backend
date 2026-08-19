"""
Tests for the pre-launch waitlist.

Split the way the rules are: the API tests cover what a client can see and do
(status codes, the response envelope, the honeypot, what the card refuses to
expose, the throttle), and the service tests cover what the data has to be
(normalisation, the sequential number, the untouched duplicate). Neither proves
the other — a serializer that quietly widens a payload passes every service
test.

``RetryTests`` forces a stale MAX so the (max + 1) collision path is actually
executed. It is the one branch no amount of ordinary traffic reaches in a test,
and the one whose failure mode is two people holding the same signup number.

The cache is cleared around every API test: the signup throttle is 5/hour per
IP and every request here comes from the same one, so a leftover bucket would
turn a later test — here or in another app — into a mystery 429.
"""

from datetime import date, timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.test import override_settings
from rest_framework.test import APITestCase

from waitlist.models import PlayerSignup
from waitlist.services.signup_services import PlayerSignupService

CREATE_URL = "/public/waitlist/players"
STATS_URL = "/public/waitlist/stats"
CARD_URL = "/public/waitlist/players/%s"


def body(**overrides):
    payload = {
        "name": "Arjun Menon",
        "phone": "9847012345",
        "district": "kozhikode",
        "position": "striker",
        "level": "club",
    }
    payload.update(overrides)
    return payload


class NormalisationTests(APITestCase):

    def test_phone_shapes(self):
        cases = {
            "9847012345": "+919847012345",
            "098470 12345": "+919847012345",
            "+91 98470-12345": "+919847012345",
            "0091 9847012345": "+919847012345",
            "919847012345": "+919847012345",
            "(984) 701-2345": "+919847012345",
            "00971501234567": "+971501234567",
            "+971 50 123 4567": "+971501234567",
            "": "",
        }
        for raw, expected in cases.items():
            self.assertEqual(
                PlayerSignupService.normalise_phone(raw), expected, raw
            )

    def test_instagram_shapes(self):
        cases = {
            "@Goatza": "goatza",
            "goatza": "goatza",
            "https://www.instagram.com/Goatza/": "goatza",
            "instagram.com/goatza?igsh=abc123": "goatza",
            "http://instagram.com/goatza/reel/xyz": "goatza",
            "www.instagram.com/goatza": "goatza",
            "": "",
        }
        for raw, expected in cases.items():
            self.assertEqual(
                PlayerSignupService.normalise_instagram(raw), expected, raw
            )

    def test_ref_code(self):
        self.assertEqual(PlayerSignupService.build_ref_code(413), "GZ0413")
        self.assertEqual(PlayerSignupService.build_ref_code(1), "GZ0001")
        self.assertEqual(PlayerSignupService.build_ref_code(10000), "GZ10000")


class ServiceTests(APITestCase):

    def setUp(self):
        cache.clear()

    def test_sequential_numbers_and_codes(self):
        first, created = PlayerSignupService.create(**body())
        self.assertTrue(created)
        self.assertEqual(first.signup_number, 1)
        self.assertEqual(first.ref_code, "GZ0001")
        self.assertEqual(first.phone, "+919847012345")
        self.assertEqual(first.state, "Kerala")
        self.assertEqual(first.sport, "football")

        second, created = PlayerSignupService.create(
            **body(name="Fathima P", phone="9995551234")
        )
        self.assertTrue(created)
        self.assertEqual(second.signup_number, 2)
        self.assertEqual(second.ref_code, "GZ0002")

    def test_repeat_phone_returns_existing_untouched(self):
        first, _ = PlayerSignupService.create(**body())

        again, created = PlayerSignupService.create(
            # Same number, typed differently, different name.
            **body(name="SOMEONE ELSE", phone="+91 98470 12345")
        )

        self.assertFalse(created)
        self.assertEqual(again.id, first.id)
        self.assertEqual(again.name, "Arjun Menon")
        self.assertEqual(PlayerSignup.objects.count(), 1)

    def test_notes_and_number_cannot_be_injected(self):
        signup, _ = PlayerSignupService.create(
            **body(notes="hi", signup_number=999, ref_code="GZ9999")
        )
        self.assertEqual(signup.notes, "")
        self.assertEqual(signup.signup_number, 1)
        self.assertEqual(signup.ref_code, "GZ0001")

    @override_settings(WAITLIST_NOTIFY_EMAIL="me@example.com")
    @patch("waitlist.services.signup_services.send_email_async")
    def test_notification_subject_and_body(self, mock_send):
        signup, _ = PlayerSignupService.create(**body(instagram="@arjun"))

        kwargs = mock_send.call_args.kwargs
        self.assertEqual(
            kwargs["subject"], "New Goatza signup #1 — Kozhikode"
        )
        self.assertEqual(kwargs["to_email"], "me@example.com")
        for expected in ("+919847012345", "@arjun", "Kozhikode", "Striker", "Club"):
            self.assertIn(expected, kwargs["message"])

    @override_settings(WAITLIST_NOTIFY_EMAIL="me@example.com")
    @patch(
        "waitlist.services.signup_services.send_email_async",
        side_effect=RuntimeError("smtp down"),
    )
    def test_mail_failure_never_fails_the_signup(self, mock_send):
        signup, created = PlayerSignupService.create(**body())
        self.assertTrue(created)
        self.assertEqual(PlayerSignup.objects.count(), 1)

    @override_settings(WAITLIST_NOTIFY_EMAIL=None)
    @patch("waitlist.services.signup_services.send_email_async")
    def test_no_recipient_configured_skips_mail(self, mock_send):
        PlayerSignupService.create(**body())
        mock_send.assert_not_called()


class APITests(APITestCase):

    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def test_create_returns_the_public_shape(self):
        response = self.client.post(
            f"{CREATE_URL}?src=ig_reel_04", body(), format="json"
        )

        self.assertEqual(response.status_code, 201)
        data = response.data["data"]
        self.assertEqual(data["signup_number"], 1)
        self.assertEqual(data["ref_code"], "GZ0001")
        self.assertEqual(data["name"], "Arjun Menon")
        self.assertEqual(data["district"], "kozhikode")
        self.assertFalse(data["already_registered"])

        self.assertEqual(PlayerSignup.objects.get().source, "ig_reel_04")

    def test_repeat_phone_is_200_not_400(self):
        self.client.post(CREATE_URL, body(), format="json")
        response = self.client.post(CREATE_URL, body(), format="json")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["success"])
        self.assertTrue(response.data["data"]["already_registered"])
        self.assertIn("#1", response.data["message"])

    def test_honeypot_looks_like_success_but_writes_nothing(self):
        response = self.client.post(
            CREATE_URL, body(website="http://spam.example"), format="json"
        )

        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.data["success"])
        self.assertEqual(
            sorted(response.data["data"].keys()),
            ["district", "name", "ref_code", "signup_number"],
        )
        self.assertEqual(PlayerSignup.objects.count(), 0)

    def test_validation(self):
        cases = [
            body(name="A"),
            body(phone="12345"),
            body(phone="1234567890123456789"),
            body(date_of_birth=str(date.today() + timedelta(days=1))),
            body(date_of_birth="1940-01-01"),
            body(district="mumbai"),
            body(position="quarterback"),
        ]
        for payload in cases:
            cache.clear()
            response = self.client.post(CREATE_URL, payload, format="json")
            self.assertEqual(response.status_code, 400, payload)
            self.assertFalse(response.data["success"])

    def test_stats(self):
        self.client.post(CREATE_URL, body(), format="json")
        response = self.client.get(STATS_URL)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["data"], {"count": 1, "goal": 1000})

    def test_stats_counter_is_busted_on_create(self):
        self.client.get(STATS_URL)  # warm the cache at 0
        self.client.post(CREATE_URL, body(), format="json")
        self.assertEqual(self.client.get(STATS_URL).data["data"]["count"], 1)

    def test_card_payload_leaks_nothing(self):
        self.client.post(
            CREATE_URL,
            body(email="a@b.com", instagram="@arjun"),
            format="json",
        )

        response = self.client.get(CARD_URL % "gz0001")  # lowercase on purpose

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            sorted(response.data["data"].keys()),
            ["district", "name", "position", "signup_number", "sport"],
        )
        self.assertNotIn("phone", str(response.data))
        self.assertNotIn("a@b.com", str(response.data))
        self.assertNotIn("arjun", str(response.data))

    def test_card_404(self):
        response = self.client.get(CARD_URL % "GZ9999")
        self.assertEqual(response.status_code, 404)
        self.assertFalse(response.data["success"])

    def test_signup_is_throttled_at_five_an_hour(self):
        for index in range(5):
            response = self.client.post(
                CREATE_URL, body(phone=f"98470123{index:02d}"), format="json"
            )
            self.assertEqual(response.status_code, 201, index)

        blocked = self.client.post(
            CREATE_URL, body(phone="9847099999"), format="json"
        )
        self.assertEqual(blocked.status_code, 429)

    def test_stats_is_not_on_the_signup_bucket(self):
        for _ in range(10):
            self.assertEqual(self.client.get(STATS_URL).status_code, 200)


class RetryTests(APITestCase):
    """The (max + 1) collision path — unreachable without forcing a stale MAX."""

    def setUp(self):
        cache.clear()
        # Occupies signup_number 1, so any attempt that reads a stale max of 0
        # collides on the unique constraint.
        PlayerSignupService.create(**body())

    def _stale_max(self, values):
        """Patch the manager's aggregate so the first read(s) return a stale max."""
        manager_class = type(PlayerSignup.objects)
        real = manager_class.aggregate
        calls = {"n": 0}

        def fake(manager, *args, **kwargs):
            index = calls["n"]
            calls["n"] += 1
            if index < len(values):
                return {"highest": values[index]}
            return real(manager, *args, **kwargs)

        return patch.object(manager_class, "aggregate", fake), calls

    def test_collision_retries_and_succeeds(self):
        patcher, calls = self._stale_max([0])

        with patcher:
            signup, created = PlayerSignupService.create(
                **body(name="Second", phone="9995551234")
            )

        self.assertTrue(created)
        self.assertEqual(signup.signup_number, 2)
        self.assertEqual(signup.ref_code, "GZ0002")
        self.assertGreaterEqual(calls["n"], 2)
        self.assertEqual(PlayerSignup.objects.count(), 2)

    def test_gives_up_after_max_attempts(self):
        from django.db import IntegrityError

        patcher, calls = self._stale_max([0] * 10)

        with patcher, self.assertRaises(IntegrityError):
            PlayerSignupService.create(**body(name="Third", phone="9995559999"))

        self.assertEqual(calls["n"], PlayerSignupService.MAX_NUMBER_ATTEMPTS)
        self.assertEqual(PlayerSignup.objects.count(), 1)
