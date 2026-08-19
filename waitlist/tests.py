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

``LocationTests`` covers the promise that a geocoding failure never costs a
signup, once per way it can fail. Every one of those paths ends in a row in the
table, which is the only assertion that really matters.

Every test that touches a number pins ``WAITLIST_DISPLAY_OFFSET``. It is read
from the environment, so a deployment value leaking into a test run would
otherwise turn "#37" into a mystery.

The cache is cleared around every API test: the signup throttle is 5/hour per
IP and every request here comes from the same one, so a leftover bucket would
turn a later test — here or in another app — into a mystery 429.
"""

from datetime import date, timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.test import override_settings
from rest_framework.test import APITestCase

from shared.models import Location
from waitlist.models import PlayerSignup
from waitlist.selectors.signup_selectors import (
    display_count,
    display_number,
    is_founding,
)
from waitlist.services.signup_services import PlayerSignupService

CREATE_URL = "/public/waitlist/players"
STATS_URL = "/public/waitlist/stats"
CARD_URL = "/public/waitlist/players/%s"

# The launch values. Pinned rather than read, so these tests describe what the
# product does rather than what someone's .env happens to say today.
OFFSET = 36
GOAL = 1000

pin_numbers = override_settings(
    WAITLIST_DISPLAY_OFFSET=OFFSET,
    WAITLIST_GOAL=GOAL,
)


def body(**overrides):
    payload = {
        "name": "Arjun Menon",
        "phone": "9847012345",
        "position": "striker",
        "level": "club",
    }
    payload.update(overrides)
    return payload


def mapbox(**overrides):
    """A Mapbox result in the shape the frontend forwards it."""
    payload = {
        "name": "Kozhikode, Kerala, India",
        "city": "Kozhikode",
        "state": "Kerala",
        "country": "India",
        "country_code": "IN",
        "latitude": 11.2588,
        "longitude": 75.7804,
        "external_id": "place.11223344",
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
            "+44 7700 900123": "+447700900123",
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
        # Takes the DISPLAY number, which is why every caller passes one in.
        self.assertEqual(PlayerSignupService.build_ref_code(413), "GZ0413")
        self.assertEqual(PlayerSignupService.build_ref_code(37), "GZ0037")
        self.assertEqual(PlayerSignupService.build_ref_code(10000), "GZ10000")


@pin_numbers
class DisplayNumberTests(APITestCase):
    """The offset, in the one place it is allowed to be arithmetic."""

    def setUp(self):
        cache.clear()

    def test_display_number_is_the_stored_number_plus_the_offset(self):
        self.assertEqual(display_number(1), 37)
        self.assertEqual(display_number(413), 449)

    def test_display_count_moves_with_the_real_count(self):
        self.assertEqual(display_count(), OFFSET)

        PlayerSignupService.create(**body())
        self.assertEqual(display_count(), OFFSET + 1)

    def test_founding_is_measured_on_the_display_number(self):
        # 964 real signups fill a 1000-strong cohort once 36 are given away.
        self.assertTrue(is_founding(1))
        self.assertTrue(is_founding(GOAL - OFFSET))
        self.assertFalse(is_founding(GOAL - OFFSET + 1))

    @override_settings(WAITLIST_DISPLAY_OFFSET=0)
    def test_offset_of_zero_is_the_honest_number(self):
        self.assertEqual(display_number(1), 1)
        self.assertTrue(is_founding(GOAL))
        self.assertFalse(is_founding(GOAL + 1))


@pin_numbers
class ServiceTests(APITestCase):

    def setUp(self):
        cache.clear()

    def test_sequential_numbers_and_codes(self):
        first, created = PlayerSignupService.create(**body())
        self.assertTrue(created)
        # The COLUMN is honest; the CODE carries the display number.
        self.assertEqual(first.signup_number, 1)
        self.assertEqual(first.ref_code, "GZ0037")
        self.assertEqual(first.phone, "+919847012345")
        self.assertEqual(first.sport, "football")

        second, created = PlayerSignupService.create(
            **body(name="Fathima P", phone="9995551234")
        )
        self.assertTrue(created)
        self.assertEqual(second.signup_number, 2)
        self.assertEqual(second.ref_code, "GZ0038")

    def test_state_has_no_default_any_more(self):
        signup, _ = PlayerSignupService.create(**body())
        self.assertEqual(signup.state, "")

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
        self.assertEqual(signup.ref_code, "GZ0037")

    @override_settings(WAITLIST_NOTIFY_EMAIL="me@example.com")
    @patch("waitlist.services.signup_services.send_email_async")
    def test_notification_subject_and_body(self, mock_send):
        PlayerSignupService.create(
            **body(instagram="@arjun", location=mapbox())
        )

        kwargs = mock_send.call_args.kwargs
        self.assertEqual(
            kwargs["subject"],
            "New Goatza signup #37 — Kozhikode, Kerala, India",
        )
        self.assertEqual(kwargs["to_email"], "me@example.com")

        message = kwargs["message"]
        for expected in (
            "+919847012345",
            "@arjun",
            "Kozhikode, Kerala, India",
            "11.2588, 75.7804",
            "Striker",
            "Club",
        ):
            self.assertIn(expected, message)

        # Both numbers, and this is the only place they appear together.
        self.assertIn("Signup #37", message)
        self.assertIn("Real number     : 1", message)

    @override_settings(WAITLIST_NOTIFY_EMAIL="me@example.com")
    @patch("waitlist.services.signup_services.send_email_async")
    def test_notification_without_a_location(self, mock_send):
        PlayerSignupService.create(**body())

        kwargs = mock_send.call_args.kwargs
        self.assertEqual(
            kwargs["subject"], "New Goatza signup #37 — No location"
        )
        self.assertIn("Coordinates     : -", kwargs["message"])

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

    def test_decoy_matches_the_real_success_shape(self):
        PlayerSignupService.create(**body())

        decoy = PlayerSignupService.decoy_payload(
            {"name": "Bot", "location": mapbox()}
        )

        self.assertEqual(
            sorted(decoy.keys()),
            ["city", "is_founding", "name", "ref_code", "signup_number"],
        )
        # One real signup exists, so the next plausible display number is 38.
        self.assertEqual(decoy["signup_number"], 38)
        self.assertEqual(decoy["ref_code"], "GZ0038")
        self.assertEqual(decoy["city"], "Kozhikode")
        self.assertTrue(decoy["is_founding"])


@pin_numbers
class LocationTests(APITestCase):
    """Location is optional at every level, and never costs a signup."""

    def setUp(self):
        cache.clear()

    def test_resolves_the_fk_and_the_denormalised_copy(self):
        signup, _ = PlayerSignupService.create(**body(location=mapbox()))

        self.assertIsNotNone(signup.location)
        self.assertEqual(signup.location_name, "Kozhikode, Kerala, India")
        self.assertEqual(signup.city, "Kozhikode")
        self.assertEqual(signup.state, "Kerala")
        self.assertEqual(signup.country_code, "IN")
        self.assertEqual(signup.latitude, 11.2588)
        self.assertEqual(signup.longitude, 75.7804)

    def test_the_fk_is_the_shared_location_row(self):
        """Two signups in the same place share one Location — not two."""
        first, _ = PlayerSignupService.create(**body(location=mapbox()))
        second, _ = PlayerSignupService.create(
            **body(name="Fathima P", phone="9995551234", location=mapbox())
        )

        self.assertEqual(Location.objects.count(), 1)
        self.assertEqual(second.location_id, first.location_id)

    def test_a_player_anywhere_in_the_world(self):
        signup, _ = PlayerSignupService.create(
            **body(
                phone="+44 7700 900123",
                location=mapbox(
                    name="Manchester, England, United Kingdom",
                    city="Manchester",
                    state="England",
                    country="United Kingdom",
                    country_code="GB",
                    latitude=53.4808,
                    longitude=-2.2426,
                    external_id="place.99887766",
                ),
            )
        )

        self.assertEqual(signup.city, "Manchester")
        self.assertEqual(signup.country_code, "GB")
        self.assertEqual(signup.phone, "+447700900123")

    def test_no_location_at_all_is_fine(self):
        signup, created = PlayerSignupService.create(**body())

        self.assertTrue(created)
        self.assertIsNone(signup.location)
        self.assertEqual(signup.city, "")
        self.assertIsNone(signup.latitude)

    def test_missing_coordinates_keep_the_text(self):
        signup, created = PlayerSignupService.create(
            **body(
                location={
                    "name": "Kozhikode, Kerala, India",
                    "city": "Kozhikode",
                }
            )
        )

        self.assertTrue(created)
        self.assertIsNone(signup.location)
        self.assertEqual(signup.location_name, "Kozhikode, Kerala, India")
        self.assertEqual(signup.city, "Kozhikode")
        self.assertIsNone(signup.latitude)

    @patch(
        "waitlist.services.signup_services.LocationService.get_or_create_location",
        side_effect=RuntimeError("geocoder down"),
    )
    def test_a_raising_geocoder_never_costs_the_signup(self, mock_resolve):
        signup, created = PlayerSignupService.create(**body(location=mapbox()))

        self.assertTrue(created)
        self.assertEqual(PlayerSignup.objects.count(), 1)
        self.assertIsNone(signup.location)
        # The text — including the coordinates the client sent — survives.
        self.assertEqual(signup.city, "Kozhikode")
        self.assertEqual(signup.state, "Kerala")
        self.assertEqual(signup.latitude, 11.2588)

    @patch(
        "waitlist.services.signup_services.LocationService.get_or_create_location",
        return_value=None,
    )
    def test_a_geocoder_that_resolves_to_nothing_is_the_same_case(self, mock_resolve):
        signup, created = PlayerSignupService.create(**body(location=mapbox()))

        self.assertTrue(created)
        self.assertIsNone(signup.location)
        self.assertEqual(signup.city, "Kozhikode")

    def test_a_location_without_a_state_does_not_blank_the_typed_one(self):
        signup, _ = PlayerSignupService.create(
            **body(
                state="Karnataka",
                location=mapbox(state="", name="Somewhere"),
            )
        )

        self.assertEqual(signup.state, "Karnataka")


@pin_numbers
class APITests(APITestCase):

    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def test_create_returns_the_public_shape(self):
        response = self.client.post(
            f"{CREATE_URL}?src=ig_reel_04",
            body(location=mapbox()),
            format="json",
        )

        self.assertEqual(response.status_code, 201)
        data = response.data["data"]
        self.assertEqual(data["signup_number"], 37)
        self.assertEqual(data["ref_code"], "GZ0037")
        self.assertEqual(data["name"], "Arjun Menon")
        self.assertEqual(data["city"], "Kozhikode")
        self.assertTrue(data["is_founding"])
        self.assertFalse(data["already_registered"])
        self.assertIn("#37", response.data["message"])

        self.assertEqual(PlayerSignup.objects.get().source, "ig_reel_04")

    def test_the_raw_number_is_never_returned(self):
        self.client.post(CREATE_URL, body(), format="json")

        # The row is #1 in the database and #37 everywhere a client can look.
        self.assertEqual(PlayerSignup.objects.get().signup_number, 1)

        card = self.client.get(CARD_URL % "GZ0037")
        self.assertEqual(card.data["data"]["signup_number"], 37)

        stats = self.client.get(STATS_URL)
        self.assertEqual(stats.data["data"]["count"], OFFSET + 1)

    def test_repeat_phone_is_200_not_400(self):
        self.client.post(CREATE_URL, body(), format="json")
        response = self.client.post(CREATE_URL, body(), format="json")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["success"])
        self.assertTrue(response.data["data"]["already_registered"])
        self.assertIn("#37", response.data["message"])

    def test_honeypot_looks_like_success_but_writes_nothing(self):
        response = self.client.post(
            CREATE_URL,
            body(website="http://spam.example", location=mapbox()),
            format="json",
        )

        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.data["success"])
        self.assertEqual(
            sorted(response.data["data"].keys()),
            ["city", "is_founding", "name", "ref_code", "signup_number"],
        )
        self.assertEqual(response.data["data"]["signup_number"], 37)
        self.assertEqual(PlayerSignup.objects.count(), 0)

    def test_validation(self):
        cases = [
            body(name="A"),
            body(phone="12345"),
            body(phone="1234567890123456789"),
            body(date_of_birth=str(date.today() + timedelta(days=1))),
            body(date_of_birth="1940-01-01"),
            body(position="quarterback"),
        ]
        for payload in cases:
            cache.clear()
            response = self.client.post(CREATE_URL, payload, format="json")
            self.assertEqual(response.status_code, 400, payload)
            self.assertFalse(response.data["success"])

    def test_a_broken_location_is_dropped_not_rejected(self):
        """Bad coordinates cost the FK, never the signup."""
        cases = [
            mapbox(latitude=999, longitude=75.7804),
            mapbox(latitude=11.2588, longitude=-500),
            mapbox(latitude="not a number", longitude="also not"),
            mapbox(latitude=None, longitude=None),
        ]

        for index, location in enumerate(cases):
            cache.clear()
            response = self.client.post(
                CREATE_URL,
                body(phone=f"98470123{index:02d}", location=location),
                format="json",
            )

            self.assertEqual(response.status_code, 201, location)

            signup = PlayerSignup.objects.get(signup_number=index + 1)
            self.assertIsNone(signup.location, location)
            self.assertIsNone(signup.latitude, location)
            # The city is still there — that is the point of dropping only the
            # coordinates.
            self.assertEqual(signup.city, "Kozhikode")

    def test_an_empty_or_junk_location_is_simply_ignored(self):
        for index, location in enumerate([{}, None, {"country_code": "IN"}]):
            cache.clear()
            response = self.client.post(
                CREATE_URL,
                body(phone=f"99955512{index:02d}", location=location),
                format="json",
            )
            self.assertEqual(response.status_code, 201, location)

        self.assertEqual(
            PlayerSignup.objects.filter(location__isnull=False).count(), 0
        )

    def test_stats(self):
        self.client.post(CREATE_URL, body(), format="json")
        response = self.client.get(STATS_URL)

        self.assertEqual(response.status_code, 200)
        # One real signup, shown as 37 — the number that signup was given.
        self.assertEqual(response.data["data"], {"count": 37, "goal": GOAL})

    def test_stats_is_never_zero(self):
        """The reason the offset exists: an empty list must not look empty."""
        response = self.client.get(STATS_URL)
        self.assertEqual(response.data["data"]["count"], OFFSET)

    def test_stats_counter_is_busted_on_create(self):
        self.client.get(STATS_URL)  # warm the cache at the offset
        self.client.post(CREATE_URL, body(), format="json")
        self.assertEqual(
            self.client.get(STATS_URL).data["data"]["count"], OFFSET + 1
        )

    def test_card_payload_leaks_nothing(self):
        self.client.post(
            CREATE_URL,
            body(email="a@b.com", instagram="@arjun", location=mapbox()),
            format="json",
        )

        response = self.client.get(CARD_URL % "gz0037")  # lowercase on purpose

        self.assertEqual(response.status_code, 200)
        data = response.data["data"]
        self.assertEqual(
            sorted(data.keys()),
            [
                "city",
                "country_code",
                "is_founding",
                "name",
                "position",
                "signup_number",
                "sport",
            ],
        )
        self.assertEqual(data["signup_number"], 37)
        self.assertEqual(data["city"], "Kozhikode")
        self.assertEqual(data["country_code"], "IN")
        self.assertTrue(data["is_founding"])

        rendered = str(response.data)
        for secret in ("phone", "a@b.com", "arjun", "11.2588", "75.7804"):
            self.assertNotIn(secret, rendered)

    def test_card_404(self):
        response = self.client.get(CARD_URL % "GZ9999")
        self.assertEqual(response.status_code, 404)
        self.assertFalse(response.data["success"])

    @override_settings(WAITLIST_GOAL=10, WAITLIST_DISPLAY_OFFSET=36)
    def test_a_player_past_the_goal_is_not_founding(self):
        """Display #37 against a goal of 10 — the cohort is already closed."""
        response = self.client.post(CREATE_URL, body(), format="json")

        self.assertFalse(response.data["data"]["is_founding"])

        card = self.client.get(CARD_URL % "GZ0037")
        self.assertFalse(card.data["data"]["is_founding"])

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


@pin_numbers
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
        self.assertEqual(signup.ref_code, "GZ0038")
        self.assertGreaterEqual(calls["n"], 2)
        self.assertEqual(PlayerSignup.objects.count(), 2)

    def test_gives_up_after_max_attempts(self):
        from django.db import IntegrityError

        patcher, calls = self._stale_max([0] * 10)

        with patcher, self.assertRaises(IntegrityError):
            PlayerSignupService.create(**body(name="Third", phone="9995559999"))

        self.assertEqual(calls["n"], PlayerSignupService.MAX_NUMBER_ATTEMPTS)
        self.assertEqual(PlayerSignup.objects.count(), 1)
