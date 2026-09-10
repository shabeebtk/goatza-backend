"""
The age gate, at every door into the product.

There are TWO ways to get an account — the email form and Google OAuth — and
the second one is the one that gets forgotten, because a Google user never sees
a signup form at all. Both are covered here, deliberately side by side, so a
future change that adds the field to one and not the other fails a test rather
than shipping a hole.

The rest is the arithmetic underneath: which country's rule applies (a
self-declared country cross-checked against the dialling code), and what
"minor" evaluates to once it does.
"""

from datetime import date, timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.constants import country_for_dialling_code, is_minor, minor_age_for
from accounts.models import User, UserProfile
from accounts.services.age_service import (
    UNDER_AGE_MESSAGE,
    AgeGateError,
    resolve_country,
    validate_signup_age,
)
from legal.testing import accept_current_terms

# accounts.urls is mounted under /user/ (see core/urls.py)
SIGNUP_URL = "/user/signup"
ROLE_URL = "/user/role"


def years_ago(years):
    """
    A birthdate that makes somebody exactly ``years`` old TODAY.

    Computed rather than hard-coded because a literal "2010-01-01" is a
    fifteen-year-old only until the calendar moves, and an age test that
    silently changes what it tests is worse than no test. The extra day keeps
    the person clear of their own birthday, so the fixture never lands on the
    boundary by accident — the boundary gets its own test below.
    """
    today = date.today()
    try:
        birthday = today.replace(year=today.year - years)
    except ValueError:
        # 29 Feb in a non-leap target year.
        birthday = today.replace(year=today.year - years, day=28)

    return birthday - timedelta(days=1)


class SignupAgeGateTests(TestCase):
    """POST /user/signup — the email form's half of the gate."""

    def setUp(self):
        # SignupThrottle is 5/min counted in the shared cache, so this class
        # inherits whatever budget the tests before it spent. Without the clear
        # a rejection test can pass for the wrong reason (429, not 400).
        cache.clear()
        self.client = APIClient()

    def _payload(self, **overrides):
        payload = {
            "name": "New Player",
            "email": "new@example.com",
            "password": "password123",
            "role": User.Role.PLAYER,
            "accepted_terms": True,
            "birthdate": years_ago(20).isoformat(),
            "country_code": "IN",
        }
        payload.update(overrides)
        return payload

    @patch("accounts.views.user_auth_views.send_signup_otp_email")
    def test_signup_stores_birthdate_and_legal_country(self, _mock_email):
        res = self.client.post(SIGNUP_URL, self._payload(), format="json")

        self.assertEqual(res.status_code, 200, res.data)

        user = User.objects.get(email="new@example.com")
        self.assertEqual(user.country_code, "IN")
        self.assertEqual(
            user.profile.birthdate, date.fromisoformat(years_ago(20).isoformat())
        )
        # An adult in India, and the property agrees.
        self.assertFalse(user.is_minor)

    @patch("accounts.views.user_auth_views.send_signup_otp_email")
    def test_signup_rejects_under_thirteen(self, mock_email):
        res = self.client.post(
            SIGNUP_URL,
            self._payload(birthdate=years_ago(12).isoformat()),
            format="json",
        )

        self.assertEqual(res.status_code, 400, res.data)
        self.assertFalse(res.data["success"])

        # THE MESSAGE IS THE TEST. It must not name the limit: "you must be 13"
        # tells a refused twelve-year-old exactly which year to retype.
        self.assertEqual(res.data["message"], UNDER_AGE_MESSAGE)
        self.assertNotIn("13", res.data["message"])
        self.assertNotIn("age", res.data["message"].lower())

        # Nothing created, nothing mailed. A refused signup must not leave a
        # half-account behind for the retry to collide with.
        self.assertFalse(User.objects.filter(email="new@example.com").exists())
        mock_email.assert_not_called()

    @patch("accounts.views.user_auth_views.send_signup_otp_email")
    def test_signup_rejects_missing_birthdate(self, mock_email):
        for value in (None, ""):
            cache.clear()
            payload = self._payload()
            if value is None:
                payload.pop("birthdate")
            else:
                payload["birthdate"] = value

            res = self.client.post(SIGNUP_URL, payload, format="json")

            self.assertEqual(res.status_code, 400, f"birthdate={value!r}")
            # A MISSING field is not an under-age refusal, and must not borrow
            # its message — that would tell an adult who forgot the field that
            # they are not allowed an account.
            self.assertNotEqual(res.data["message"], UNDER_AGE_MESSAGE)

        self.assertFalse(User.objects.filter(email="new@example.com").exists())
        mock_email.assert_not_called()

    @patch("accounts.views.user_auth_views.send_signup_otp_email")
    def test_signup_rejects_missing_or_malformed_country(self, mock_email):
        # "" and a missing key are absence; "INDIA" and "1" are not alpha-2.
        # All four are refused, because a country that cannot be normalized
        # cannot be looked up and would silently take the strict default while
        # claiming to be the user's answer.
        for value in (None, "", "INDIA", "1"):
            cache.clear()
            payload = self._payload()
            if value is None:
                payload.pop("country_code")
            else:
                payload["country_code"] = value

            res = self.client.post(SIGNUP_URL, payload, format="json")

            self.assertEqual(res.status_code, 400, f"country_code={value!r}")

        self.assertFalse(User.objects.filter(email="new@example.com").exists())
        mock_email.assert_not_called()

    @patch("accounts.views.user_auth_views.send_signup_otp_email")
    def test_signup_rejects_an_impossible_birthdate(self, _mock_email):
        # A future date and an 1899 one are typos, not attempts — and they get
        # a message that says so, unlike the under-age refusal above.
        for value in ((date.today() + timedelta(days=1)).isoformat(), "1899-01-01"):
            cache.clear()

            res = self.client.post(
                SIGNUP_URL, self._payload(birthdate=value), format="json"
            )

            self.assertEqual(res.status_code, 400, f"birthdate={value!r}")
            self.assertNotEqual(res.data["message"], UNDER_AGE_MESSAGE)

        self.assertFalse(User.objects.filter(email="new@example.com").exists())

    @patch("accounts.views.user_auth_views.send_signup_otp_email")
    def test_exactly_thirteen_today_is_allowed(self, _mock_email):
        # The floor is "under 13 is refused", not "13 is refused". Somebody who
        # turns 13 today is in.
        res = self.client.post(
            SIGNUP_URL,
            self._payload(birthdate=date.today().replace(
                year=date.today().year - 13
            ).isoformat()),
            format="json",
        )

        self.assertEqual(res.status_code, 200, res.data)
        # ...and is still a minor in India, which is the whole point of the two
        # thresholds being separate numbers.
        self.assertTrue(User.objects.get(email="new@example.com").is_minor)


class GoogleRoleAgeGateTests(TestCase):
    """
    POST /user/role — the OAuth half, and the easy one to miss.

    A Google account reaches this endpoint with no password, no role, no
    consent and no birthdate, because the only thing its owner clicked was on
    Google's screen. If the gate were not here, every Google signup would walk
    past it.
    """

    def setUp(self):
        cache.clear()
        self.client = APIClient()

    def _google_user(self, **profile_kwargs):
        """A freshly created Google user: consented, but no age on file."""
        user = User.objects.create_user(
            email="oauth@example.com",
            username="oauth",
            password="password123",
            is_role_confirmed=False,
        )
        accept_current_terms(user)
        UserProfile.objects.create(user=user, name="OAuth User", **profile_kwargs)
        return user

    def test_role_is_refused_without_a_birthdate(self):
        user = self._google_user()
        self.client.force_authenticate(user=user)

        res = self.client.post(
            ROLE_URL, {"role": "coach", "country_code": "IN"}, format="json"
        )

        self.assertEqual(res.status_code, 400, res.data)
        self.assertFalse(res.data["success"])

        # THE INVARIANT: no confirmed role without an age on file. A user who
        # got past this would be inside the app, indistinguishable from an
        # adult, forever.
        user.refresh_from_db()
        self.assertFalse(user.is_role_confirmed)

    def test_role_is_refused_without_a_country(self):
        user = self._google_user()
        self.client.force_authenticate(user=user)

        res = self.client.post(
            ROLE_URL,
            {"role": "coach", "birthdate": years_ago(20).isoformat()},
            format="json",
        )

        self.assertEqual(res.status_code, 400, res.data)
        user.refresh_from_db()
        self.assertFalse(user.is_role_confirmed)

    def test_role_is_refused_for_an_under_thirteen_google_user(self):
        user = self._google_user()
        self.client.force_authenticate(user=user)

        res = self.client.post(
            ROLE_URL,
            {
                "role": "player",
                "birthdate": years_ago(11).isoformat(),
                "country_code": "GB",
            },
            format="json",
        )

        self.assertEqual(res.status_code, 400, res.data)
        # The SAME neutral message as the form. The two paths must not be
        # distinguishable by their refusals either.
        self.assertEqual(res.data["message"], UNDER_AGE_MESSAGE)

        user.refresh_from_db()
        self.assertFalse(user.is_role_confirmed)
        self.assertEqual(user.country_code, "")
        self.assertIsNone(user.profile.birthdate)

    def test_role_stores_birthdate_and_country_on_success(self):
        user = self._google_user()
        self.client.force_authenticate(user=user)

        res = self.client.post(
            ROLE_URL,
            {
                "role": "player",
                "birthdate": years_ago(15).isoformat(),
                "country_code": "in",  # lowercase: normalized on the way in
            },
            format="json",
        )

        self.assertEqual(res.status_code, 200, res.data)

        user.refresh_from_db()
        self.assertTrue(user.is_role_confirmed)
        self.assertEqual(user.country_code, "IN")
        self.assertEqual(user.profile.birthdate, years_ago(15))
        self.assertTrue(user.is_minor)
        self.assertTrue(res.data["data"]["is_minor"])

    def test_a_user_who_already_has_an_age_is_not_asked_again(self):
        # An email signup passing through, or a Google user changing role a
        # second time. The gate is a one-time capture, not a per-request toll.
        user = self._google_user(birthdate=years_ago(25))
        user.country_code = "GB"
        user.save(update_fields=["country_code"])
        self.client.force_authenticate(user=user)

        res = self.client.post(ROLE_URL, {"role": "scout"}, format="json")

        self.assertEqual(res.status_code, 200, res.data)
        user.refresh_from_db()
        self.assertEqual(user.role, "scout")
        self.assertEqual(user.country_code, "GB")


class ResolveCountryTests(TestCase):
    """
    The declared country is a dropdown; the dialling code is a cross-check.

    The rule is asymmetric on purpose: a phone number may only ever make the
    answer STRICTER. See the module docstring of accounts/services/age_service.
    """

    def test_phone_wins_when_it_is_stricter(self):
        # The attack this exists to stop: a 15-year-old on an Indian number
        # (consent age 18) selecting "GB" (13) to switch the regime off.
        self.assertEqual(resolve_country("GB", "+919876543210"), "IN")
        self.assertGreater(minor_age_for("IN"), minor_age_for("GB"))

    def test_declaration_wins_when_the_phone_is_looser(self):
        # The mirror image, and it must NOT flip: an Indian who happens to hold
        # a UK number is still held to India's 18.
        self.assertEqual(resolve_country("IN", "+447700900123"), "IN")

    def test_declaration_wins_on_a_tie(self):
        # DE and NL are both 16, so there is nothing to gain by overriding —
        # and relocating somebody for no protective benefit would be wrong.
        self.assertEqual(resolve_country("DE", "+31612345678"), "DE")

    def test_no_phone_returns_the_declaration(self):
        for phone in (None, "", "   "):
            self.assertEqual(resolve_country("GB", phone), "GB")

    def test_an_unreadable_number_returns_the_declaration(self):
        # National format (no "+") carries no country code, and +999 is not in
        # the table. Both are "no information", not "no country".
        self.assertEqual(resolve_country("GB", "9876543210"), "GB")
        self.assertEqual(resolve_country("GB", "+9991234567"), "GB")

    def test_longer_dialling_codes_are_matched_first(self):
        # Asserted on the lookup itself rather than through resolve_country,
        # which would confound a prefix bug with the stricter-wins rule.
        # "+353" is Ireland and must never be read as "+35" (nothing) or "+3";
        # "+91" is India and must never be read as "+9".
        self.assertEqual(country_for_dialling_code("+353871234567"), "IE")
        self.assertEqual(country_for_dialling_code("+919876543210"), "IN")
        self.assertEqual(country_for_dialling_code("+12125550123"), "US")

    def test_the_declaration_is_normalized(self):
        self.assertEqual(resolve_country("in", None), "IN")
        # Not a country code at all → "", which reads as the strict default.
        self.assertEqual(resolve_country("INDIA", None), "")


class IsMinorTests(TestCase):
    """The same person, two jurisdictions, two answers."""

    def test_fifteen_is_a_minor_in_india_and_not_in_the_uk(self):
        fifteen = years_ago(15)

        # India: DPDP puts the age of digital consent at 18.
        self.assertTrue(is_minor(fifteen, "IN"))
        # UK: 13 under the UK GDPR. Same person, same day, different answer —
        # which is why there is a table here and not a constant.
        self.assertFalse(is_minor(fifteen, "GB"))

    def test_an_unknown_birthdate_reads_as_a_minor(self):
        # THE default that matters. Every row predating the signup gate has
        # birthdate=None, and treating "we never asked" as "adult" would apply
        # adult treatment to an unknown number of children.
        self.assertTrue(is_minor(None, "GB"))
        self.assertTrue(is_minor(None, "IN"))
        self.assertTrue(is_minor(None, ""))

    def test_an_unresearched_country_gets_the_strict_default(self):
        # ZW is not in the table. A 17-year-old there is a minor because the
        # default is 18 — safe before anyone has looked the rule up.
        self.assertTrue(is_minor(years_ago(17), "ZW"))
        self.assertFalse(is_minor(years_ago(19), "ZW"))

    def test_the_boundary_is_exclusive(self):
        # "Minor" is UNDER the threshold. Turning 18 in India ends it.
        self.assertTrue(is_minor(years_ago(17), "IN"))
        self.assertFalse(is_minor(years_ago(18), "IN"))

    def test_user_property_reads_both_tables(self):
        user = User.objects.create_user(
            email="prop@example.com",
            username="prop",
            password="password123",
            country_code="IN",
        )
        UserProfile.objects.create(
            user=user, name="Prop", birthdate=years_ago(15)
        )
        self.assertTrue(user.is_minor)

        user.country_code = "GB"
        self.assertFalse(user.is_minor)

    def test_user_property_does_not_raise_without_a_profile(self):
        # A User with no profile row is reachable (staff-made users, fixtures,
        # accounts created before the profile write existed). The property must
        # answer, not explode — and the answer is the safe one.
        user = User.objects.create_user(
            email="noprofile@example.com",
            username="noprofile",
            password="password123",
            country_code="GB",
        )
        self.assertTrue(user.is_minor)


class ValidateSignupAgeTests(TestCase):
    """The service call both views share, at its own boundary."""

    def test_under_thirteen_raises_with_the_neutral_message(self):
        with self.assertRaises(AgeGateError) as ctx:
            validate_signup_age(years_ago(12), "GB")

        self.assertEqual(ctx.exception.error_code, "under_age")
        self.assertEqual(str(ctx.exception.detail[0]), UNDER_AGE_MESSAGE)

    def test_the_floor_ignores_the_local_consent_age(self):
        # GB's consent age is 13 and India's is 18, but the floor is 13
        # everywhere — a 15-year-old is a minor in India and still gets an
        # account. The two rules are independent.
        self.assertIsNone(validate_signup_age(years_ago(15), "IN"))

    def test_a_future_birthdate_is_a_typo_not_an_under_age_refusal(self):
        # Order matters: a future date yields a NEGATIVE age, which would sail
        # through the under-13 comparison and hand a slip the message reserved
        # for a refusal.
        with self.assertRaises(AgeGateError) as ctx:
            validate_signup_age(date.today() + timedelta(days=1), "IN")

        self.assertEqual(ctx.exception.error_code, "invalid_birthdate")

    def test_an_implausibly_old_birthdate_is_rejected(self):
        with self.assertRaises(AgeGateError) as ctx:
            validate_signup_age(date(1899, 12, 31), "IN")

        self.assertEqual(ctx.exception.error_code, "invalid_birthdate")

    def test_a_missing_birthdate_is_rejected(self):
        with self.assertRaises(AgeGateError) as ctx:
            validate_signup_age(None, "IN")

        self.assertEqual(ctx.exception.error_code, "invalid_birthdate")


class BirthdateCorrectionTests(TestCase):
    """
    One self-serve correction, then the support route.

    The limit is what stops the birthdate being a preference rather than an
    answer. The support route is what keeps the legal right to correct
    inaccurate personal data intact — see the docstring on
    UpdateUserProfileSerializer.validate_birthdate.
    """

    PROFILE_URL = "/user/update/profile/data"

    def setUp(self):
        cache.clear()
        self.client = APIClient()

        self.user = User.objects.create_user(
            email="corr@example.com",
            username="corr",
            password="password123",
            country_code="IN",
        )
        accept_current_terms(self.user)
        UserProfile.objects.create(
            user=self.user, name="Corr", birthdate=years_ago(20)
        )
        self.client.force_authenticate(user=self.user)

    def _patch_birthdate(self, birthdate):
        return self.client.patch(
            self.PROFILE_URL,
            {"birthdate": birthdate.isoformat()},
            format="json",
        )

    def test_the_first_correction_is_allowed_and_counted(self):
        res = self._patch_birthdate(years_ago(21))

        self.assertEqual(res.status_code, 200, res.data)

        self.user.refresh_from_db()
        self.assertEqual(self.user.profile.birthdate, years_ago(21))
        self.assertEqual(self.user.birthdate_corrections, 1)

    def test_the_second_correction_is_refused(self):
        self._patch_birthdate(years_ago(21))
        res = self._patch_birthdate(years_ago(22))

        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn("birthdate", res.data["data"])
        self.assertIn(
            "Contact support", str(res.data["data"]["birthdate"])
        )

        # The refused value must not have landed.
        self.user.refresh_from_db()
        self.assertEqual(self.user.profile.birthdate, years_ago(21))
        self.assertEqual(self.user.birthdate_corrections, 1)

    def test_resaving_the_same_date_does_not_spend_the_correction(self):
        # The profile form PATCHes every field it holds, so an unrelated edit
        # routinely carries the unchanged birthdate along with it. That must
        # not cost the user their one correction.
        res = self.client.patch(
            self.PROFILE_URL,
            {"headline": "Striker", "birthdate": years_ago(20).isoformat()},
            format="json",
        )

        self.assertEqual(res.status_code, 200, res.data)
        self.user.refresh_from_db()
        self.assertEqual(self.user.birthdate_corrections, 0)

    def test_country_code_cannot_be_changed_through_the_profile_editor(self):
        # The jurisdiction is not editable here at all — a user who could flip
        # IN to GB after signup would have the age gate's own hole, reopened on
        # a screen with no age check on it.
        res = self.client.patch(
            self.PROFILE_URL, {"country_code": "GB"}, format="json"
        )

        self.user.refresh_from_db()
        self.assertEqual(self.user.country_code, "IN")
        # Whether the unknown key is ignored or rejected, the column is what
        # matters — and it is untouched either way.
        self.assertIn(res.status_code, (200, 400))
