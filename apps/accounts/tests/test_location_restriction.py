"""
A profile says which town somebody is in. Never which building.

The rule has two halves and this file tests both, because either one alone is
not a rule:

  * the PICKER searches in `city` mode, so the only things a user can see are
    localities, taluks, panchayats and postal towns — but a picker restricts a
    browser, and a PATCH does not have to come from one. The serializer is what
    makes it a rule.
  * the rows written BEFORE that rule are still precise. The rule would
    otherwise be true for new users and false for existing ones, so
    `downgrade_precise_locations` walks the ones already on file.

It applies to every user, minor and adult alike. Nothing about a sports profile
needs a doorstep.
"""

from io import StringIO
from unittest.mock import patch

from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.models import User, UserProfile
from apps.legal.testing import accept_current_terms
from apps.places.services.places_service import PlacesUnavailable, PlacesUpstreamError
from shared.models import Location

PROFILE_URL = "/user/update/profile/data"

# The command under test, for patching its two Google-facing names.
COMMAND = "apps.accounts.management.commands.downgrade_precise_locations"


def city_payload(**overrides):
    """The payload the profile picker sends: a town, with its place id."""
    payload = {
        "provider": "google",
        "name": "Panoor",
        "type": "city",
        "city": "Panoor",
        "state": "Kerala",
        "country": "India",
        "country_code": "IN",
        "latitude": 11.8,
        "longitude": 75.6,
        "external_id": "place_panoor",
    }
    payload.update(overrides)
    return payload


class ProfileLocationTypeTests(TestCase):
    """PATCH /user/update/profile/data — the server-side half of the rule."""

    def setUp(self):
        cache.clear()
        self.client = APIClient()

        self.user = User.objects.create_user(
            email="player@example.com",
            username="player",
            password="password123",
            country_code="IN",
        )
        accept_current_terms(self.user)
        UserProfile.objects.create(user=self.user, name="Player")
        self.client.force_authenticate(user=self.user)

    def _patch_location(self, location):
        return self.client.patch(
            PROFILE_URL, {"location": location}, format="json"
        )

    def _location_error(self, res):
        """
        The view wraps DRF's error dict one level down, in ``data`` — see
        response_data() in utils. Reading it through a helper keeps the tests
        about the rule rather than about the envelope.
        """
        return [str(e) for e in (res.data.get("data") or {}).get("location", [])]

    def test_a_town_is_accepted(self):
        res = self._patch_location(city_payload())

        self.assertEqual(res.status_code, 200, res.data)

        profile = UserProfile.objects.get(user=self.user)
        self.assertEqual(profile.location.type, Location.Type.CITY)
        self.assertEqual(profile.city, "Panoor")
        self.assertEqual(profile.latitude, 11.8)

    def test_a_precise_place_is_rejected(self):
        """
        The crafted request the picker cannot produce: a real Google place id
        for a building, posted straight at the endpoint.
        """
        res = self._patch_location(city_payload(
            name="Fortune Apartments, Beach Road",
            type="place",
            external_id="place_building",
        ))

        self.assertEqual(res.status_code, 400)
        self.assertEqual(
            self._location_error(res), ["Please select a town or city."]
        )

        # And nothing was written on the way to the rejection.
        profile = UserProfile.objects.get(user=self.user)
        self.assertIsNone(profile.location)
        self.assertFalse(
            Location.objects.filter(external_id="place_building").exists()
        )

    def test_a_payload_with_no_type_is_rejected(self):
        """
        An omitted type is not a neutral default. LocationService.normalize_data
        fills a missing one in with `place`, so accepting the payload here would
        be accepting exactly what the rule refuses.
        """
        payload = city_payload()
        payload.pop("type")

        res = self._patch_location(payload)

        self.assertEqual(res.status_code, 400)
        self.assertEqual(
            self._location_error(res), ["Please select a town or city."]
        )

    def test_clearing_the_location_still_works(self):
        """
        `null` is not a location of the wrong type — it is no location, which
        every user is entitled to.
        """
        self._patch_location(city_payload())

        res = self.client.patch(
            PROFILE_URL, {"location": None}, format="json"
        )

        self.assertEqual(res.status_code, 200, res.data)

        profile = UserProfile.objects.get(user=self.user)
        self.assertIsNone(profile.location)
        self.assertEqual(profile.city, "")

    def test_the_rule_does_not_depend_on_being_a_minor(self):
        """
        Nobody's exact whereabouts is needed for a sports profile, and an
        adult's home address is not less theirs for being an adult's.
        """
        from datetime import date

        self.user.profile.birthdate = date(1990, 1, 1)
        self.user.profile.save(update_fields=["birthdate"])

        res = self._patch_location(city_payload(type="place"))

        self.assertEqual(res.status_code, 400)


class DowngradePreciseLocationsTests(TestCase):
    """
    The rows that predate the rule.

    Google is mocked throughout: what is under test is which rows are picked
    up, what each one becomes and whether a second run is a no-op — not
    whether Google can find Panoor.
    """

    def setUp(self):
        cache.clear()

        self.precise = Location.objects.create(
            name="Fortune Apartments, Beach Road",
            type=Location.Type.PLACE,
            provider=Location.Provider.GOOGLE,
            city="Thalassery",
            state="Kerala",
            country="India",
            country_code="IN",
            latitude=11.7481,
            longitude=75.4929,
            external_id="place_building",
        )

    def _profile(self, email, location, **overrides):
        user = User.objects.create_user(
            email=email, username=email.split("@")[0], password="password123"
        )
        fields = {
            "name": "Player",
            "location": location,
            "location_name": location.name,
            "city": location.city,
            "country_code": location.country_code,
            "latitude": location.latitude,
            "longitude": location.longitude,
        }
        fields.update(overrides)
        return UserProfile.objects.create(user=user, **fields)

    def _google(self, results=None, place=None):
        """Patch the command's two Google-facing calls. Returns both mocks."""
        autocomplete = patch(
            f"{COMMAND}.autocomplete",
            return_value={"results": results if results is not None else [{
                "place_id": "place_thalassery",
                "name": "Thalassery",
                "label": "Thalassery, Kerala, India",
                "secondary": "Kerala, India",
                "types": ["administrative_area_level_3"],
            }]},
        )
        details = patch(
            f"{COMMAND}.details",
            return_value=place if place is not None else {
                "place_id": "place_thalassery",
                "latitude": 11.7401,
                "longitude": 75.4900,
                "city": "Thalassery",
                "state": "Kerala",
                "country": "India",
                "country_code": "IN",
                "types": ["administrative_area_level_3"],
            },
        )
        return autocomplete, details

    def _run(self, *args):
        out, err = StringIO(), StringIO()
        call_command(
            "downgrade_precise_locations", *args, stdout=out, stderr=err
        )
        return out.getvalue() + err.getvalue()

    # ── The happy path ───────────────────────────────────────────────────────

    def test_a_precise_profile_is_re_pointed_at_its_town(self):
        profile = self._profile("precise@example.com", self.precise)

        auto, det = self._google()

        with auto, det:
            output = self._run()

        profile.refresh_from_db()

        self.assertEqual(profile.location.type, Location.Type.CITY)
        self.assertEqual(profile.location.external_id, "place_thalassery")
        self.assertEqual(profile.location_name, "Thalassery")
        self.assertEqual(profile.city, "Thalassery")
        self.assertEqual(profile.latitude, 11.7401)
        self.assertIn("converted=1", output)

    def test_the_town_is_searched_for_by_the_rows_own_address_columns(self):
        """
        Not by its name. "Fortune Apartments, Beach Road" is the precise thing
        being moved away from; city/state/country are what Google itself said
        the place sits inside.
        """
        self._profile("precise@example.com", self.precise)

        auto, det = self._google()

        with auto as autocomplete_mock, det:
            self._run()

        self.assertEqual(
            autocomplete_mock.call_args.kwargs["q"],
            "Thalassery, Kerala, India",
        )
        self.assertEqual(autocomplete_mock.call_args.kwargs["mode"], "city")

    def test_one_lookup_serves_every_profile_on_the_same_place(self):
        """A block of flats is routinely on several profiles. Pay once."""
        for i in range(3):
            self._profile(f"flat{i}@example.com", self.precise)

        auto, det = self._google()

        with auto as autocomplete_mock, det:
            output = self._run()

        self.assertEqual(autocomplete_mock.call_count, 1)
        self.assertIn("converted=3", output)

    def test_a_city_profile_is_left_alone(self):
        city = Location.objects.create(
            name="Kannur",
            type=Location.Type.CITY,
            city="Kannur",
            state="Kerala",
            country="India",
            country_code="IN",
            latitude=11.87,
            longitude=75.37,
            external_id="place_kannur",
        )
        profile = self._profile("city@example.com", city)

        auto, det = self._google()

        with auto as autocomplete_mock, det:
            self._run()

        profile.refresh_from_db()

        self.assertEqual(profile.location_id, city.id)
        self.assertEqual(autocomplete_mock.call_count, 0)

    # ── The fallback ─────────────────────────────────────────────────────────

    def test_a_row_with_no_city_loses_its_point_and_keeps_its_label(self):
        orphan = Location.objects.create(
            name="Unnamed Ground",
            type=Location.Type.PLACE,
            city="",
            state="",
            country="",
            country_code="IN",
            latitude=11.0,
            longitude=75.0,
            external_id="place_orphan",
        )
        profile = self._profile(
            "orphan@example.com", orphan, city="", location_name="Unnamed Ground"
        )

        auto, det = self._google()

        with auto as autocomplete_mock, det:
            output = self._run()

        profile.refresh_from_db()

        self.assertIsNone(profile.location)
        self.assertIsNone(profile.latitude)
        self.assertIsNone(profile.longitude)
        # The text fallback survives: the profile still reads as somewhere.
        self.assertEqual(profile.location_name, "Unnamed Ground")
        self.assertEqual(profile.country_code, "IN")
        # Nothing to search with, so nothing was spent asking.
        self.assertEqual(autocomplete_mock.call_count, 0)
        self.assertIn("nulled=1", output)

    def test_a_town_google_cannot_find_nulls_rather_than_staying_precise(self):
        profile = self._profile("nothing@example.com", self.precise)

        auto, det = self._google(results=[])

        with auto, det:
            output = self._run()

        profile.refresh_from_db()

        self.assertIsNone(profile.location)
        self.assertIsNone(profile.latitude)
        self.assertEqual(profile.city, "Thalassery")
        self.assertIn("nulled=1", output)

    def test_a_failed_lookup_nulls_and_is_reported(self):
        profile = self._profile("boom@example.com", self.precise)

        auto = patch(f"{COMMAND}.autocomplete", side_effect=PlacesUpstreamError("nope"))
        _, det = self._google()

        with auto, det:
            output = self._run()

        profile.refresh_from_db()

        self.assertIsNone(profile.location)
        self.assertIn("lookup_failures=1", output)

    def test_an_answer_as_precise_as_the_input_is_refused(self):
        """
        If the "town" comes back as the very place being moved away from, it is
        not a parent — and taking it would leave the run with work it thinks it
        has finished.
        """
        profile = self._profile("same@example.com", self.precise)

        auto, det = self._google(results=[{
            "place_id": self.precise.external_id,
            "name": "Fortune Apartments",
            "label": "Fortune Apartments, Beach Road",
            "secondary": "",
            "types": ["premise"],
        }])

        with auto, det:
            self._run()

        profile.refresh_from_db()
        self.assertIsNone(profile.location)

    def test_an_outage_stops_the_run_instead_of_nulling_the_rest(self):
        """
        Coordinates a later run could have kept are not thrown away because
        Google was down for a minute.
        """
        profile = self._profile("outage@example.com", self.precise)

        auto = patch(f"{COMMAND}.autocomplete", side_effect=PlacesUnavailable("off"))
        _, det = self._google()

        with auto, det:
            output = self._run()

        profile.refresh_from_db()

        self.assertEqual(profile.location_id, self.precise.id)
        self.assertIn("stopped early", output)

    # ── Running it twice ─────────────────────────────────────────────────────

    def test_a_second_run_is_a_no_op(self):
        converted = self._profile("precise@example.com", self.precise)
        nulled = self._profile(
            "orphan@example.com",
            Location.objects.create(
                name="Unnamed Ground",
                type=Location.Type.PLACE,
                city="",
                country_code="IN",
                latitude=11.0,
                longitude=75.0,
                external_id="place_orphan",
            ),
        )

        auto, det = self._google()

        with auto as autocomplete_mock, det:
            self._run()

            converted.refresh_from_db()
            nulled.refresh_from_db()
            after_first = (
                converted.location_id,
                converted.latitude,
                nulled.location_id,
                nulled.latitude,
            )
            calls_after_first = autocomplete_mock.call_count

            output = self._run()

            converted.refresh_from_db()
            nulled.refresh_from_db()

            self.assertEqual(
                (
                    converted.location_id,
                    converted.latitude,
                    nulled.location_id,
                    nulled.latitude,
                ),
                after_first,
            )
            self.assertEqual(autocomplete_mock.call_count, calls_after_first)
            self.assertIn("profiles to downgrade: 0", output)

    # ── Dry run ──────────────────────────────────────────────────────────────

    def test_dry_run_writes_nothing_and_calls_nobody(self):
        profile = self._profile("precise@example.com", self.precise)

        auto, det = self._google()

        with auto as autocomplete_mock, det:
            output = self._run("--dry-run")

        profile.refresh_from_db()

        self.assertEqual(profile.location_id, self.precise.id)
        self.assertEqual(profile.latitude, 11.7481)
        self.assertEqual(autocomplete_mock.call_count, 0)
        self.assertIn("resolvable=1", output)
