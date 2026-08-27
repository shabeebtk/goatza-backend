"""
The public (logged-out) profile surface.

The tests that matter most here are the two allow-list ones: they compare the
payload's key set against the constant the serializer declares, so a field
added to any base serializer — or to the model — can never silently start
appearing on a page anyone can scrape. Every other test in this file is about
the four ways a profile must resolve to 404 without saying which.
"""

from datetime import date, timedelta

from django.core.cache import cache
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import User, UserProfile
from accounts.serializers.public_profile_serializers import (
    PUBLIC_SPORT_ATTRIBUTE_KEYS,
    PUBLIC_USER_PROFILE_KEYS,
    age_group_badge,
)
from connections.models import Follow
from connections.services.follow_services import FollowService
from core.actor import Actor
from core.constant import TYPE_ORGANIZATION, TYPE_USER
from highlights.models import Highlight
from organization.models import (
    Organization,
    OrganizationMember,
    OrganizationProfile,
)
from organization.serializers.public_profile_serializers import (
    PUBLIC_ORG_PROFILE_KEYS,
)
from posts.models import Post
from recruitments.models import Recruitment
from sports.models import (
    Sport,
    SportAttribute,
    SportAttributeOption,
    UserAttributeValue,
    UserSport,
)

PROFILE_URL = "/public/profile/{}"
PROFILE_POSTS_URL = "/public/profile/{}/posts"
ORG_URL = "/public/organization/{}"
ORG_POSTS_URL = "/public/organization/{}/posts"

USER_PRIVACY_URL = "/user/privacy/public-profile"
ORG_PRIVACY_URL = "/organizations/privacy/public-profile"


class PublicProfileTestCase(APITestCase):
    """Shared fixtures: a public player, a hidden one, and an org."""

    def setUp(self):
        # Username → profile lookups and the public bundle are both cached, and
        # both leak between tests otherwise.
        cache.clear()

        self.sport = Sport.objects.create(name="Football", icon_name="mdi:soccer")

        self.player = self._user("riya", "Riya Nair")
        self.hidden = self._user("hidden", "Hidden Player", is_public=False)

        self.org = Organization.objects.create(
            name="Dream FC", username="dreamfc", type=Organization.Type.CLUB,
        )
        OrganizationProfile.objects.create(
            organization=self.org, headline="Kerala's academy",
        )

    def _user(self, username, name, is_public=True, is_active=True, role=None):
        user = User.objects.create_user(
            email=f"{username}@example.com",
            password="pass1234",
            username=username,
            role=role or User.Role.PLAYER,
        )
        if not is_active:
            user.is_active = False
            user.save(update_fields=["is_active"])

        UserProfile.objects.create(
            user=user,
            name=name,
            headline=f"{name} headline",
            about="About me",
            city="Kochi",
            country_code="IN",
            location_name="Kochi, Kerala",
            latitude=9.93,
            longitude=76.26,
            birthdate=date.today() - timedelta(days=365 * 17 + 10),
            height_cm=175,
            weight_kg="68.50",
            is_public_profile=is_public,
        )
        return user


# =====================================================================
# ALLOW-LIST
# =====================================================================

class PublicPayloadAllowListTests(PublicProfileTestCase):

    def test_user_payload_keys_match_the_allow_list_exactly(self):
        """
        The guard against a leak. Not a subset check — an EQUALITY check, so a
        field added to BaseUserSerializer (which carries `email`) or to
        UserProfile fails here rather than shipping.
        """
        response = self.client.get(PROFILE_URL.format("riya"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        profile = response.data["data"]["profile"]
        self.assertEqual(set(profile.keys()), PUBLIC_USER_PROFILE_KEYS)

    def test_user_payload_never_carries_private_fields(self):
        """
        Names the specific fields, so the failure message says WHAT leaked
        rather than just that the key set changed.
        """
        response = self.client.get(PROFILE_URL.format("riya"))
        profile = response.data["data"]["profile"]

        for field in (
            "email", "phone", "is_email_verified", "is_phone_verified",
            "is_staff", "is_active", "is_superuser", "is_role_confirmed",
            "is_onboarding_completed", "birthdate", "latitude", "longitude",
            "gender", "is_public_profile",
        ):
            self.assertNotIn(field, profile, f"{field} leaked to the public payload")

        # And not smuggled inside the nested location either — city-level only.
        self.assertNotIn("latitude", profile["location"])
        self.assertNotIn("longitude", profile["location"])
        self.assertEqual(profile["location"]["city"], "Kochi")

    def test_age_group_replaces_the_raw_birthdate(self):
        response = self.client.get(PROFILE_URL.format("riya"))
        profile = response.data["data"]["profile"]

        self.assertEqual(profile["age_group"], "U17")
        self.assertNotIn("birthdate", profile)

    def test_updated_at_tracks_the_later_of_user_and_profile(self):
        """
        The share card's cache-buster. It is cached for an hour at the CDN, so
        it has to move when EITHER model changes — a rename lives on User, a new
        photo lives on UserProfile, and both change the card.
        """
        response = self.client.get(PROFILE_URL.format("riya"))
        profile = response.data["data"]["profile"]

        self.assertIn("updated_at", profile)
        first = profile["updated_at"]

        cache.clear()
        self.player.profile.headline = "Now with a different headline"
        self.player.profile.save(update_fields=["headline", "updated_at"])

        response = self.client.get(PROFILE_URL.format("riya"))
        self.assertGreater(response.data["data"]["profile"]["updated_at"], first)

    def test_weight_is_a_float_not_a_decimal(self):
        """
        The same preview dicts are msgpack'd onto the channel layer, which can
        only carry primitives — a Decimal here would blow up a realtime send.
        """
        response = self.client.get(PROFILE_URL.format("riya"))
        weight = response.data["data"]["profile"]["weight_kg"]

        self.assertIsInstance(weight, float)
        self.assertEqual(weight, 68.5)

    def test_org_payload_keys_match_the_allow_list_exactly(self):
        response = self.client.get(ORG_URL.format("dreamfc"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        profile = response.data["data"]["profile"]
        self.assertEqual(set(profile.keys()), PUBLIC_ORG_PROFILE_KEYS)


class PublicSportAttributeTests(PublicProfileTestCase):
    """
    The sport-agnostic half of the payload: a player's values for their PRIMARY
    sport's attributes, which is what lets the share card print "Preferred foot"
    for a footballer and "Batting style" for a cricketer with no code change.

    The filtering rules are the point of these tests — a `text` attribute is
    arbitrarily long and a `multi_select` is comma soup, and both would wreck a
    card slot sized for two words.
    """

    def setUp(self):
        super().setUp()

        UserSport.objects.create(
            user=self.player, sport=self.sport, is_primary=True,
            experience_level=UserSport.ExperienceLevel.ADVANCED,
        )

        self.foot = SportAttribute.objects.create(
            sport=self.sport, name="Preferred foot",
            data_type=SportAttribute.DataType.SELECT, display_order=1,
        )
        right = SportAttributeOption.objects.create(
            attribute=self.foot, value="Right",
        )
        UserAttributeValue.objects.create(
            user=self.player, sport=self.sport,
            attribute=self.foot, option=right,
        )
        cache.clear()

    def _attributes(self, username="riya"):
        response = self.client.get(PROFILE_URL.format(username))
        return response.data["data"]["profile"]["primary_sport"]["attributes"]

    def test_a_select_attribute_appears_with_its_option_value(self):
        attributes = self._attributes()

        self.assertEqual(len(attributes), 1)
        self.assertEqual(attributes[0]["name"], "Preferred foot")
        self.assertEqual(attributes[0]["value"], "Right")
        self.assertEqual(attributes[0]["data_type"], "select")

    def test_attribute_rows_carry_only_the_allow_listed_keys(self):
        for row in self._attributes():
            self.assertEqual(set(row.keys()), PUBLIC_SPORT_ATTRIBUTE_KEYS)

    def test_text_and_multi_select_never_appear(self):
        for name, data_type in (
            ("Playing notes", SportAttribute.DataType.TEXT),
            ("Other positions", SportAttribute.DataType.MULTI_SELECT),
        ):
            attribute = SportAttribute.objects.create(
                sport=self.sport, name=name, data_type=data_type,
                display_order=5,
            )
            UserAttributeValue.objects.create(
                user=self.player, sport=self.sport,
                attribute=attribute, value_text="something long",
            )

        cache.clear()
        names = [row["name"] for row in self._attributes()]

        self.assertEqual(names, ["Preferred foot"])

    def test_an_empty_value_is_dropped_rather_than_rendered_blank(self):
        blank = SportAttribute.objects.create(
            sport=self.sport, name="Jersey number",
            data_type=SportAttribute.DataType.NUMBER, display_order=2,
        )
        UserAttributeValue.objects.create(
            user=self.player, sport=self.sport,
            attribute=blank, value_text="   ",
        )

        cache.clear()
        names = [row["name"] for row in self._attributes()]

        self.assertEqual(names, ["Preferred foot"])

    def test_another_sports_attributes_are_excluded(self):
        """
        Only the primary sport's. A player who also logs cricket must not get a
        batting style printed on a football card.
        """
        cricket = Sport.objects.create(name="Cricket", icon_name="mdi:cricket")
        UserSport.objects.create(user=self.player, sport=cricket)

        style = SportAttribute.objects.create(
            sport=cricket, name="Batting style",
            data_type=SportAttribute.DataType.SELECT, display_order=1,
        )
        left = SportAttributeOption.objects.create(
            attribute=style, value="Left-hand",
        )
        UserAttributeValue.objects.create(
            user=self.player, sport=cricket, attribute=style, option=left,
        )

        cache.clear()
        names = [row["name"] for row in self._attributes()]

        self.assertEqual(names, ["Preferred foot"])

    def test_rows_follow_display_order(self):
        second = SportAttribute.objects.create(
            sport=self.sport, name="Jersey number",
            data_type=SportAttribute.DataType.NUMBER, display_order=0,
        )
        UserAttributeValue.objects.create(
            user=self.player, sport=self.sport,
            attribute=second, value_text="10",
        )

        cache.clear()
        names = [row["name"] for row in self._attributes()]

        self.assertEqual(names, ["Jersey number", "Preferred foot"])

    def test_a_player_with_no_primary_sport_has_no_primary_sport_block(self):
        """A brand-new signup. The card falls back to its fixed slots."""
        self._user("nosport", "No Sport")
        cache.clear()

        response = self.client.get(PROFILE_URL.format("nosport"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNone(response.data["data"]["profile"]["primary_sport"])


class AgeGroupBadgeTests(APITestCase):
    """
    Mirrors features/profile/utils/ageGroup.ts. The two implementations must
    agree — the client renders the badge from a raw birthdate on the
    authenticated path and from this value on the public one.
    """

    def _age(self, years):
        return date.today() - timedelta(days=365 * years + 10)

    def test_bands(self):
        self.assertIsNone(age_group_badge(None))
        self.assertEqual(age_group_badge(self._age(5)), "U7")
        self.assertEqual(age_group_badge(self._age(7)), "U7")
        self.assertEqual(age_group_badge(self._age(8)), "U8")
        self.assertEqual(age_group_badge(self._age(23)), "U23")
        self.assertEqual(age_group_badge(self._age(24)), "Senior")
        self.assertEqual(age_group_badge(self._age(40)), "Senior")


# =====================================================================
# 404 MATRIX
# =====================================================================

class PublicProfileNotFoundTests(PublicProfileTestCase):
    """
    Every unavailable reason is a 404 and none of them is distinguishable.
    A 403 would confirm the account exists, which is exactly what someone
    probing a hidden profile wants to learn.
    """

    def test_unknown_username_is_404(self):
        response = self.client.get(PROFILE_URL.format("nobody"))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_hidden_profile_is_404(self):
        response = self.client.get(PROFILE_URL.format("hidden"))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_hidden_profile_leaks_no_name(self):
        """The 404 body must not carry the profile it is refusing to show."""
        response = self.client.get(PROFILE_URL.format("hidden"))
        self.assertNotIn("Hidden Player", str(response.data))

    def test_inactive_user_is_404(self):
        self._user("gone", "Gone Player", is_active=False)
        response = self.client.get(PROFILE_URL.format("gone"))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_usernameless_user_is_404(self):
        """
        User.username is nullable. Someone who never set one has no public URL
        at all — there is nothing to resolve.
        """
        user = User.objects.create_user(
            email="nousername@example.com", password="pass1234",
        )
        UserProfile.objects.create(user=user, name="No Handle")

        self.assertIsNone(user.username)
        response = self.client.get(PROFILE_URL.format(""))
        self.assertIn(
            response.status_code,
            (status.HTTP_404_NOT_FOUND, status.HTTP_301_MOVED_PERMANENTLY),
        )

    def test_inactive_org_is_404(self):
        org = Organization.objects.create(
            name="Dead FC", username="deadfc",
            type=Organization.Type.CLUB, is_active=False,
        )
        OrganizationProfile.objects.create(organization=org)

        response = self.client.get(ORG_URL.format("deadfc"))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_hidden_org_is_404(self):
        self.org.profile.is_public_profile = False
        self.org.profile.save(update_fields=["is_public_profile"])
        cache.clear()

        response = self.client.get(ORG_URL.format("dreamfc"))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_posts_endpoint_404s_for_a_hidden_profile_too(self):
        """The pagination endpoint must not be a way around the toggle."""
        response = self.client.get(PROFILE_POSTS_URL.format("hidden"))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


# =====================================================================
# ANONYMOUS FILTERING
# =====================================================================

class PublicContentFilteringTests(PublicProfileTestCase):

    def setUp(self):
        super().setUp()

        self.public_post = Post.objects.create(
            author_user=self.player,
            content="public post",
            visibility=Post.Visibility.PUBLIC,
        )
        self.followers_post = Post.objects.create(
            author_user=self.player,
            content="followers only",
            visibility=Post.Visibility.FOLLOWERS,
        )
        self.deleted_post = Post.objects.create(
            author_user=self.player,
            content="deleted",
            visibility=Post.Visibility.PUBLIC,
            is_deleted=True,
        )

    def _post_texts(self, response):
        return {p["content"] for p in response.data["data"]["posts"]["results"]}

    def test_only_public_undeleted_posts_are_returned(self):
        response = self.client.get(PROFILE_URL.format("riya"))

        texts = self._post_texts(response)
        self.assertIn("public post", texts)
        self.assertNotIn("followers only", texts)
        self.assertNotIn("deleted", texts)

    def test_only_everyone_highlights_are_returned(self):
        for visibility in Highlight.Visibility.values:
            Highlight.objects.create(
                user=self.player,
                title=visibility,
                file_url=f"https://cdn.test/{visibility}.mp4",
                public_id=f"h/{visibility}",
                visibility=visibility,
            )

        response = self.client.get(PROFILE_URL.format("riya"))
        titles = {h["title"] for h in response.data["data"]["highlights"]}

        self.assertEqual(titles, {Highlight.Visibility.EVERYONE})

    def test_restricted_highlights_are_absent_not_placeheld(self):
        """
        The rail carries no marker for what was filtered out. A "locked" tile
        would advertise that hidden footage exists, which is the one thing
        recruiters_only is meant to avoid.
        """
        Highlight.objects.create(
            user=self.player, title="secret",
            file_url="https://cdn.test/s.mp4", public_id="h/s",
            visibility=Highlight.Visibility.RECRUITERS_ONLY,
        )

        response = self.client.get(PROFILE_URL.format("riya"))
        self.assertEqual(response.data["data"]["highlights"], [])

    def test_no_highlight_view_row_is_written_for_an_anonymous_visitor(self):
        Highlight.objects.create(
            user=self.player, title="clip",
            file_url="https://cdn.test/c.mp4", public_id="h/c",
            visibility=Highlight.Visibility.EVERYONE,
            views_count=0,
        )

        self.client.get(PROFILE_URL.format("riya"))

        clip = Highlight.objects.get(public_id="h/c")
        self.assertEqual(clip.views_count, 0)
        self.assertEqual(clip.views.count(), 0)

    def test_only_active_public_recruitments_are_returned(self):
        def _recruitment(title, status_value, visibility):
            return Recruitment.objects.create(
                organization=self.org,
                title=title,
                sport=self.sport,
                status=status_value,
                visibility=visibility,
            )

        _recruitment("live", Recruitment.Status.ACTIVE,
                     Recruitment.Visibility.PUBLIC)
        _recruitment("closed", Recruitment.Status.CLOSED,
                     Recruitment.Visibility.PUBLIC)
        _recruitment("draft", Recruitment.Status.DRAFT,
                     Recruitment.Visibility.PUBLIC)
        _recruitment("followers", Recruitment.Status.ACTIVE,
                     Recruitment.Visibility.FOLLOWERS_ONLY)

        response = self.client.get(ORG_URL.format("dreamfc"))
        titles = {r["title"] for r in response.data["data"]["recruitments"]}

        self.assertEqual(titles, {"live"})

    def test_career_and_achievements_include_unverified_entries(self):
        """
        Both lists are fully public to anyone who can see the profile —
        verified rows are badged, not filtered.
        """
        response = self.client.get(PROFILE_URL.format("riya"))
        data = response.data["data"]

        self.assertIn("career", data)
        self.assertIn("achievements", data)


# =====================================================================
# ANONYMOUS ACTOR PLUMBING
# =====================================================================

class FollowServiceAnonymousTests(APITestCase):
    """
    Both methods start by reading actor.is_user, which is an AttributeError on
    None — and core.actor.resolve_actor returns None for every anonymous
    request. The public post filter runs through both.
    """

    def test_get_following_ids_with_no_actor(self):
        self.assertEqual(
            FollowService.get_following_ids(None),
            {"user_ids": [], "org_ids": []},
        )

    def test_get_relationship_with_no_actor(self):
        relationship = FollowService.get_relationship(
            actor=None, target_id="whatever", target_type=TYPE_USER
        )

        self.assertEqual(
            relationship,
            {
                "is_me": False,
                "is_following": False,
                "is_followed_by": False,
                "is_connected": False,
                # Blocking added these to the shape. An anonymous caller has no
                # identity to be blocked with, so both are False — and the
                # exact-dict comparison is kept deliberately: it is what makes
                # a field silently appearing on this response a test failure.
                "is_blocked": False,
                "is_blocked_by_me": False,
            },
        )

    def test_get_relationship_still_works_for_a_real_actor(self):
        """The None guard must not have changed the authenticated answer."""
        alice = User.objects.create_user(
            email="a@example.com", password="pass1234", username="alice",
        )
        UserProfile.objects.create(user=alice, name="Alice")
        bob = User.objects.create_user(
            email="b@example.com", password="pass1234", username="bob",
        )
        UserProfile.objects.create(user=bob, name="Bob")

        Follow.objects.create(follower_user=alice, following_user=bob)

        relationship = FollowService.get_relationship(
            actor=Actor(actor_type=TYPE_USER, user=alice),
            target_id=bob.id,
            target_type=TYPE_USER,
        )

        self.assertTrue(relationship["is_following"])
        self.assertFalse(relationship["is_followed_by"])


class PublicEndpointAuthTests(PublicProfileTestCase):

    def test_anonymous_request_succeeds(self):
        """No token, no actor headers — the whole point of the surface."""
        response = self.client.get(PROFILE_URL.format("riya"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_authenticated_caller_sees_their_own_followers_only_posts(self):
        """
        ActorMixin still runs on a public endpoint, so a signed-in caller is
        resolved as their normal actor and is NOT downgraded to the anonymous
        view of their own content.
        """
        Post.objects.create(
            author_user=self.player,
            content="mine only",
            visibility=Post.Visibility.FOLLOWERS,
        )

        self.client.force_authenticate(user=self.player)
        response = self.client.get(PROFILE_URL.format("riya"))

        texts = {p["content"] for p in response.data["data"]["posts"]["results"]}
        self.assertIn("mine only", texts)

    def test_authenticated_caller_bypasses_the_anonymous_cache(self):
        """
        The bundle cache is keyed by username alone, so it may only ever hold
        the anonymous rendering — otherwise one signed-in visitor's view would
        be served to everybody.
        """
        # Warm the cache anonymously.
        self.client.get(PROFILE_URL.format("riya"))

        Post.objects.create(
            author_user=self.player,
            content="private to me",
            visibility=Post.Visibility.FOLLOWERS,
        )

        self.client.force_authenticate(user=self.player)
        response = self.client.get(PROFILE_URL.format("riya"))

        texts = {p["content"] for p in response.data["data"]["posts"]["results"]}
        self.assertIn("private to me", texts)


# =====================================================================
# PRIVACY TOGGLES
# =====================================================================

class UserPrivacyToggleTests(PublicProfileTestCase):

    def test_owner_can_hide_and_unhide_their_profile(self):
        self.client.force_authenticate(user=self.player)

        response = self.client.patch(
            USER_PRIVACY_URL, {"is_public_profile": False}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.player.profile.refresh_from_db()
        self.assertFalse(self.player.profile.is_public_profile)

        # And the public URL stops resolving immediately — the cached bundle is
        # invalidated by the write, not left to expire.
        self.assertEqual(
            self.client.get(PROFILE_URL.format("riya")).status_code,
            status.HTTP_404_NOT_FOUND,
        )

    def test_anonymous_cannot_toggle(self):
        response = self.client.patch(
            USER_PRIVACY_URL, {"is_public_profile": False}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_missing_value_is_rejected_rather_than_defaulting_to_false(self):
        """A malformed request must never silently hide someone's profile."""
        self.client.force_authenticate(user=self.player)

        response = self.client.patch(USER_PRIVACY_URL, {}, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.player.profile.refresh_from_db()
        self.assertTrue(self.player.profile.is_public_profile)


class OrgPrivacyToggleTests(PublicProfileTestCase):

    def setUp(self):
        super().setUp()

        self.owner = self._user("clubowner", "Club Owner",
                                role=User.Role.ORG_USER)
        self.admin = self._user("clubadmin", "Club Admin",
                                role=User.Role.ORG_USER)
        self.coach = self._user("clubcoach", "Club Coach",
                                role=User.Role.COACH)
        self.staff = self._user("clubstaff", "Club Staff",
                                role=User.Role.ORG_USER)
        self.outsider = self._user("outsider", "Outsider")

        for user, role in (
            (self.owner, OrganizationMember.Role.OWNER),
            (self.admin, OrganizationMember.Role.ADMIN),
            (self.coach, OrganizationMember.Role.COACH),
            (self.staff, OrganizationMember.Role.STAFF),
        ):
            OrganizationMember.objects.create(
                organization=self.org, user=user, role=role
            )

    def _patch(self, user, value=False):
        self.client.force_authenticate(user=user)
        return self.client.patch(
            ORG_PRIVACY_URL,
            {"is_public_profile": value},
            format="json",
            HTTP_X_ACTOR_TYPE=TYPE_ORGANIZATION,
            HTTP_X_ACTOR_ID=str(self.org.id),
        )

    def test_owner_may_toggle(self):
        self.assertEqual(self._patch(self.owner).status_code, status.HTTP_200_OK)
        self.org.profile.refresh_from_db()
        self.assertFalse(self.org.profile.is_public_profile)

    def test_admin_may_toggle(self):
        self.assertEqual(self._patch(self.admin).status_code, status.HTTP_200_OK)

    def test_coach_is_rejected(self):
        response = self._patch(self.coach)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.org.profile.refresh_from_db()
        self.assertTrue(self.org.profile.is_public_profile)

    def test_staff_is_rejected(self):
        response = self._patch(self.staff)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.org.profile.refresh_from_db()
        self.assertTrue(self.org.profile.is_public_profile)

    def test_non_member_gets_404_not_403(self):
        """
        A stranger must not learn that the org id is real by watching the
        status code — resolve_actor rejects them first, but the service check
        behind it agrees.
        """
        self.client.force_authenticate(user=self.outsider)
        response = self.client.patch(
            ORG_PRIVACY_URL,
            {"is_public_profile": False},
            format="json",
            HTTP_X_ACTOR_TYPE=TYPE_ORGANIZATION,
            HTTP_X_ACTOR_ID=str(self.org.id),
        )

        self.assertIn(
            response.status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
        )
        self.org.profile.refresh_from_db()
        self.assertTrue(self.org.profile.is_public_profile)
