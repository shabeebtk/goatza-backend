"""
The two-tier public profile: a minor's stripped card.

THE CLAIM UNDER TEST, in one sentence: a child's profile is still reachable and
still indexed, and what a logged-out stranger gets from it is a name, a
headline, two counts, a sport and a district.

Both halves matter and each is a different failure. Stripping too little
publishes a scouting profile of a named child to anyone who can type a URL.
Stripping too much — or de-indexing them — quietly delivers a platform that
works for adults and not for the young players it was built for, which is the
failure nobody would notice for a year.

WHY THE FIXTURES CARRY A COUNTRY
"Minor" is per-jurisdiction (accounts/constants.py): 18 in India, 13 in the UK.
So the pairs below are the SAME AGE in two countries rather than two ages, and
the tests assert on the treatment, not on the number. That is also what stops
this file from silently passing if the age table is edited.
"""

from datetime import date, timedelta

from django.core.cache import cache
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import User, UserProfile
from apps.accounts.serializers.public_profile_serializers import (
    PUBLIC_USER_PROFILE_KEYS,
)
from apps.cv.models import PlayerCVSettings
from apps.legal.testing import accept_current_terms
from apps.posts.models import Post
from apps.sports.models import (
    Sport,
    SportAttribute,
    SportAttributeOption,
    UserAttributeValue,
    UserSport,
)

PROFILE_URL = "/public/profile/{}"
PROFILE_POSTS_URL = "/public/profile/{}/posts"
CV_URL = "/public/cv/{}"
SITEMAP_URL = "/public/sitemap/urls"


def years_ago(years):
    """A birthdate making somebody exactly `years` old today, plus a day's
    clearance so a fixture never lands on its own birthday boundary."""
    today = date.today()
    try:
        birthday = today.replace(year=today.year - years)
    except ValueError:  # 29 Feb into a non-leap year
        birthday = today.replace(year=today.year - years, day=28)
    return birthday - timedelta(days=1)


class MinorPublicProfileTestCase(APITestCase):
    """
    Two 15-year-olds and one adult.

    ``minor`` is Indian (consent age 18 → a minor). ``adult_same_age`` is the
    same age in the UK (13 → not a minor), which is the control that proves the
    stripping keys off the JURISDICTION rule and not off something incidental
    like "has a birthdate at all".
    """

    def setUp(self):
        # The username lookup and the whole public bundle are both cached, and
        # both leak between tests otherwise.
        cache.clear()

        self.sport = Sport.objects.create(
            name="Football", icon_name="mdi:soccer"
        )
        # The attribute is a property of the SPORT, not of a player — unique on
        # (sport, name) — so it is built once here and each user below only
        # gets their own value for it.
        self.foot = SportAttribute.objects.create(
            sport=self.sport, name="Preferred foot",
            data_type="select", display_order=1,
        )
        self.right = SportAttributeOption.objects.create(
            attribute=self.foot, value="Right",
        )

        self.minor = self._user("arjun", "Arjun Nair", country_code="IN")
        self.adult_same_age = self._user(
            "riya", "Riya Nair", country_code="GB"
        )

        for user in (self.minor, self.adult_same_age):
            self._give_sport(user)

    # ── fixtures ─────────────────────────────────────────────

    def _user(self, username, name, *, country_code, age=15):
        user = User.objects.create_user(
            email=f"{username}@example.com",
            password="pass1234",
            username=username,
            role=User.Role.PLAYER,
            country_code=country_code,
        )
        accept_current_terms(user)

        UserProfile.objects.create(
            user=user,
            name=name,
            headline=f"{name} headline",
            about="I train at St Xavier's every Tuesday",
            profile_photo="https://media.example.com/u/photo.webp",
            cover_photo="https://media.example.com/u/cover.webp",
            city="Kannur",
            country_code="IN",
            # A sub-district place — the precision the minor rule drops.
            location_name="Panoor, Kannur, Kerala",
            latitude=11.87,
            longitude=75.37,
            birthdate=years_ago(age),
            height_cm=168,
            weight_kg="55.50",
            followers_count=12,
            following_count=8,
            is_public_profile=True,
        )
        return user

    def _give_sport(self, user):
        UserSport.objects.create(
            user=user, sport=self.sport,
            experience_level="intermediate", is_primary=True,
        )
        UserAttributeValue.objects.create(
            user=user, sport=self.sport,
            attribute=self.foot, option=self.right,
        )

    # ── helpers ──────────────────────────────────────────────

    def _profile(self, username):
        response = self.client.get(PROFILE_URL.format(username))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return response.data["data"]["profile"]


class MinorIsStrippedTests(MinorPublicProfileTestCase):

    def test_the_profile_still_resolves(self):
        # The premise of the whole design. If this ever 404s, the stripped card
        # has quietly become a hidden profile and the tests below are testing
        # nothing.
        response = self.client.get(PROFILE_URL.format("arjun"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_the_key_set_is_unchanged(self):
        """
        A minor's payload has the SAME KEYS as an adult's, emptied — never
        fewer. Dropping keys would make the allow-list guard a subset check for
        some users, and a subset check is what stops catching leaks.
        """
        self.assertEqual(
            set(self._profile("arjun").keys()), PUBLIC_USER_PROFILE_KEYS
        )

    def test_photos_are_withheld(self):
        profile = self._profile("arjun")

        self.assertEqual(profile["profile_photo"], "")
        self.assertEqual(profile["cover_photo"], "")

    def test_measurements_are_withheld(self):
        profile = self._profile("arjun")

        self.assertIsNone(profile["height_cm"])
        self.assertIsNone(profile["weight_kg"])

    def test_age_group_is_withheld(self):
        """
        The badge would read "U15" here. A U-band beside a named child on an
        anonymous page lets anyone scraping the site sort children by year
        group, and the helper goes down to U7 — below our own signup floor.
        """
        self.assertIsNone(self._profile("arjun")["age_group"])

    def test_about_is_withheld(self):
        # The fixture's bio names a school. Nothing can filter that field by
        # rule, so the whole field goes.
        profile = self._profile("arjun")

        self.assertEqual(profile["about"], "")
        self.assertNotIn("Xavier", str(profile))

    def test_sport_attribute_values_are_withheld(self):
        profile = self._profile("arjun")

        self.assertIsNotNone(profile["primary_sport"])
        self.assertEqual(profile["primary_sport"]["attributes"], [])

    def test_the_shown_fields_survive(self):
        """
        The other half of the design. A card with nothing on it is a de-indexed
        profile wearing a disguise — these are the fields that make being
        listed worth anything.
        """
        profile = self._profile("arjun")

        self.assertEqual(profile["name"], "Arjun Nair")
        self.assertEqual(profile["username"], "arjun")
        self.assertEqual(profile["headline"], "Arjun Nair headline")
        self.assertEqual(profile["followers_count"], 12)
        self.assertEqual(profile["following_count"], 8)
        self.assertEqual(profile["primary_sport"]["sport"], "Football")
        self.assertTrue(profile["created_at"])

    def test_the_client_is_told_the_view_is_limited(self):
        profile = self._profile("arjun")

        self.assertTrue(profile["is_minor"])
        self.assertTrue(profile["is_limited_view"])


class MinorLocationTests(MinorPublicProfileTestCase):

    def test_location_drops_to_the_city(self):
        """
        Name + sport + "Panoor" (a village) narrows to a handful of children.
        Name + sport + "Kannur" (a district) does not. The `name` key stays so
        the client renders one label without branching — it just carries the
        coarser value.
        """
        location = self._profile("arjun")["location"]

        self.assertEqual(location["city"], "Kannur")
        self.assertEqual(location["name"], "Kannur")
        self.assertNotIn("Panoor", str(location))
        self.assertEqual(location["country_code"], "IN")

    def test_coordinates_are_still_absent(self):
        # Already true for everyone; asserted here so a minor is never the
        # accidental exception.
        location = self._profile("arjun")["location"]

        self.assertNotIn("latitude", location)
        self.assertNotIn("longitude", location)


class MinorPostsTests(MinorPublicProfileTestCase):

    def setUp(self):
        super().setUp()
        for user in (self.minor, self.adult_same_age):
            for i in range(3):
                Post.objects.create(author_user=user, content=f"Post {i}")
        cache.clear()

    def test_the_posts_endpoint_returns_an_empty_page_not_a_404(self):
        """
        404 would contradict the profile page, which loads fine — the client
        would render an error state for a profile that resolved.
        """
        response = self.client.get(PROFILE_POSTS_URL.format("arjun"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["data"]["results"], [])
        # Zero, not the real total: "sign in to see 47 posts" advertises how
        # much is being withheld about one named child.
        self.assertEqual(response.data["data"]["count"], 0)

    def test_the_bundle_carries_no_posts_either(self):
        # The copy a crawler actually indexes. Stripping only the paginated
        # endpoint would leave a page of a child's posts in the server-rendered
        # HTML.
        posts = self._bundle_posts("arjun")

        self.assertEqual(posts["results"], [])
        self.assertEqual(posts["count"], 0)

    def test_an_adult_still_gets_their_posts(self):
        response = self.client.get(PROFILE_POSTS_URL.format("riya"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["data"]["count"], 3)
        self.assertEqual(len(self._bundle_posts("riya")["results"]), 3)

    def _bundle_posts(self, username):
        response = self.client.get(PROFILE_URL.format(username))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.data["data"]["posts"]


class MinorCVTests(MinorPublicProfileTestCase):

    def setUp(self):
        super().setUp()
        for user in (self.minor, self.adult_same_age):
            PlayerCVSettings.objects.create(user=user, is_enabled=True)
        cache.clear()

    def test_a_minors_cv_is_the_same_404_as_an_unknown_username(self):
        """
        Not a 403. Telling a prober "this exists but is protected because the
        owner is a child" is worse than telling them nothing — it confirms both
        the account and the age.
        """
        enabled = self.client.get(CV_URL.format("arjun"))
        nobody = self.client.get(CV_URL.format("no-such-user"))

        self.assertEqual(enabled.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(nobody.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(enabled.data["message"], nobody.data["message"])

    def test_an_adults_cv_still_works(self):
        response = self.client.get(CV_URL.format("riya"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)


class MinorIndexingTests(MinorPublicProfileTestCase):
    """
    THE DELIBERATE NON-EXCLUSION. Minors stay in the sitemap.

    If somebody "fixes" the visibility predicate to drop them, this fails — and
    the comment block in core/selectors/public_profile_selectors.py explains
    why that is a product decision rather than a tidy-up.
    """

    def test_a_minor_appears_in_the_sitemap_feed(self):
        cache.clear()
        response = self.client.get(SITEMAP_URL)

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        usernames = {
            row["username"] for row in response.data["data"]["users"]
        }
        self.assertIn("arjun", usernames)
        self.assertIn("riya", usernames)


class AdultIsUnaffectedTests(MinorPublicProfileTestCase):
    """
    The regression guard. The same age in a jurisdiction with a lower consent
    age is NOT a minor, and nothing about their payload changes.
    """

    def test_the_adult_payload_is_untouched(self):
        profile = self._profile("riya")

        self.assertEqual(
            profile["profile_photo"], "https://media.example.com/u/photo.webp"
        )
        self.assertEqual(
            profile["cover_photo"], "https://media.example.com/u/cover.webp"
        )
        self.assertEqual(profile["height_cm"], 168)
        self.assertEqual(profile["weight_kg"], 55.5)
        self.assertEqual(profile["age_group"], "U15")
        self.assertEqual(profile["about"], "I train at St Xavier's every Tuesday")
        self.assertEqual(
            profile["primary_sport"]["attributes"][0]["value"], "Right"
        )

    def test_the_adult_keeps_their_sub_district_location(self):
        location = self._profile("riya")["location"]

        self.assertEqual(location["name"], "Panoor, Kannur, Kerala")
        self.assertEqual(location["city"], "Kannur")

    def test_the_adult_is_not_flagged_as_limited(self):
        profile = self._profile("riya")

        self.assertFalse(profile["is_minor"])
        self.assertFalse(profile["is_limited_view"])

    def test_an_account_with_no_birthdate_is_treated_as_a_minor(self):
        """
        Every row predating the signup gate has birthdate=None, and
        constants.is_minor reads that as a minor. So the stripped card is the
        DEFAULT for legacy accounts, not the exception — worth pinning, because
        it is a large population and the safe answer is the surprising one.
        """
        legacy = User.objects.create_user(
            email="legacy@example.com", password="pass1234",
            username="legacy", role=User.Role.PLAYER,
        )
        accept_current_terms(legacy)
        UserProfile.objects.create(
            user=legacy, name="Legacy Player", city="Kochi",
            profile_photo="https://media.example.com/u/legacy.webp",
            is_public_profile=True,
        )
        cache.clear()

        profile = self._profile("legacy")

        self.assertTrue(profile["is_limited_view"])
        self.assertEqual(profile["profile_photo"], "")
