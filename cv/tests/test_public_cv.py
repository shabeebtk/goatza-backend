"""
The public Sports CV surface.

Modelled on accounts/tests/test_public_profile.py, and the same two tests carry
most of the weight:

  * the allow-list test, which compares the payload's key set against the
    constant the serializers declare, so a field added anywhere upstream can
    never silently start appearing on a page anyone can scrape; and

  * the 404 matrix, which asserts the four unavailable reasons are not merely
    all 404 but BYTE-IDENTICAL — a body that differed by a word would tell a
    prober which of the four they had hit.

Everything else here is about the three things that make a CV different from a
profile: the toggles remove data rather than hide it, the contact block is a
phone number and never an email, and the highlights rail is `everyone`-only for
every viewer including a signed-in scout.
"""

from datetime import date, timedelta

from django.core.cache import cache
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import User, UserProfile
from accounts.serializers.public_profile_serializers import (
    PUBLIC_USER_PROFILE_KEYS,
)
from core.constant import TYPE_ORGANIZATION
from cv.models import PlayerCVSettings
from cv.serializers.cv_serializers import (
    CV_CONTACT_KEYS,
    CV_PAYLOAD_ALWAYS_KEYS,
    CV_PAYLOAD_KEYS,
)
from highlights.models import Highlight
from organization.models import (
    Organization,
    OrganizationMember,
    OrganizationProfile,
)
from sports.models import (
    Sport,
    SportAttribute,
    SportAttributeOption,
    UserAttributeValue,
    UserSport,
)
from legal.testing import accept_current_terms

CV_URL = "/public/cv/{}"
PROFILE_URL = "/public/profile/{}"
CV_SETTINGS_URL = "/user/cv/settings"
USER_PRIVACY_URL = "/user/privacy/public-profile"


class CVTestCase(APITestCase):
    """Shared fixtures: one player with an enabled CV, plus the near misses."""

    def setUp(self):
        # The CV bundle, the profile bundle and the per-IP view latch are all
        # cached, and all three leak between tests otherwise.
        cache.clear()

        self.sport = Sport.objects.create(
            name="Football", icon_name="mdi:soccer"
        )

        self.player = self._user("riya", "Riya Nair", phone="+919000000001")
        self.settings = self._cv(self.player)

        UserSport.objects.create(
            user=self.player, sport=self.sport, is_primary=True,
            experience_level=UserSport.ExperienceLevel.ADVANCED,
        )

        foot = SportAttribute.objects.create(
            sport=self.sport, name="Preferred foot",
            data_type=SportAttribute.DataType.SELECT, display_order=1,
        )
        right = SportAttributeOption.objects.create(
            attribute=foot, value="Right",
        )
        UserAttributeValue.objects.create(
            user=self.player, sport=self.sport, attribute=foot, option=right,
        )

    # ── fixtures ─────────────────────────────────────────────

    def _user(self, username, name, is_public=True, is_active=True,
              role=None, phone=None):
        user = User.objects.create_user(
            email=f"{username}@example.com",
            password="pass1234",
            username=username,
            phone=phone,
            role=role or User.Role.PLAYER,
        )
        accept_current_terms(user)
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

    def _cv(self, user, **overrides):
        return PlayerCVSettings.objects.create(
            user=user, is_enabled=True, **overrides
        )

    # ── helpers ──────────────────────────────────────────────

    def _set(self, **fields):
        """Flip toggles on the fixture player and drop the cached CV."""
        for field, value in fields.items():
            setattr(self.settings, field, value)
        self.settings.save(update_fields=list(fields) + ["updated_at"])
        cache.clear()

    def _get(self, username="riya", ip="10.0.0.1"):
        """
        One CV read. Callers vary ``ip`` on purpose: the view counter is
        deduplicated per IP, and every request from Django's test client
        otherwise arrives from 127.0.0.1.
        """
        return self.client.get(CV_URL.format(username), REMOTE_ADDR=ip)

    def _data(self, **kwargs):
        response = self._get(**kwargs)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.data["data"]


# =====================================================================
# ALLOW-LIST
# =====================================================================

class CVPayloadAllowListTests(CVTestCase):

    def test_payload_keys_match_the_allow_list_exactly(self):
        """
        Everything switched on, so the payload is at its widest — and then an
        EQUALITY check against the declared constant, not a subset check. A
        section added upstream has to be added to the allow-list here
        deliberately before it can reach a public page.
        """
        self._set(show_contact=True)
        data = self._data()

        self.assertEqual(set(data.keys()), CV_PAYLOAD_KEYS)

    def test_the_default_payload_stays_inside_the_allow_list(self):
        """With the defaults, the optional sections that are off are absent."""
        data = self._data()

        self.assertLess(set(data.keys()), CV_PAYLOAD_KEYS)
        self.assertLessEqual(CV_PAYLOAD_ALWAYS_KEYS, set(data.keys()))

    def test_header_reuses_the_public_profile_allow_list(self):
        """
        The header IS PublicUserProfileSerializer, unwidened. If the CV ever
        needs a field the profile does not carry, it gets its own CV-scoped
        serializer — it does not get bolted onto the shared one.
        """
        profile = self._data()["profile"]

        self.assertEqual(set(profile.keys()), PUBLIC_USER_PROFILE_KEYS)

    def test_payload_never_carries_private_fields(self):
        """Names the fields, so a failure says WHAT leaked."""
        data = self._data()
        rendered = str(data)

        for field in (
            "email", "is_email_verified", "is_phone_verified", "is_staff",
            "is_active", "is_superuser", "is_role_confirmed",
            "is_onboarding_completed", "birthdate", "latitude", "longitude",
            "gender", "is_public_profile",
        ):
            self.assertNotIn(
                field, data["profile"],
                f"{field} leaked to the public CV payload",
            )

        # And the account's own email nowhere in the whole payload, contact
        # block included.
        self.assertNotIn("riya@example.com", rendered)

    def test_age_group_replaces_the_raw_birthdate(self):
        """
        Spec §2.5 asked for "age (from DOB)". Age group only: full name plus an
        exact date of birth on a scrapeable page is the identity-theft pair,
        and most of these players are minors.
        """
        profile = self._data()["profile"]

        self.assertEqual(profile["age_group"], "U17")
        self.assertNotIn("birthdate", profile)
        self.assertNotIn("age", profile)


# =====================================================================
# 404 MATRIX
# =====================================================================

class CVNotFoundTests(CVTestCase):
    """
    Every unavailable reason is the same 404. A 403 — or a differently-worded
    404 — would confirm which of them applied, which is exactly what someone
    probing a disabled CV wants to learn.
    """

    def test_disabled_cv_is_404(self):
        self._set(is_enabled=False)

        self.assertEqual(
            self._get().status_code, status.HTTP_404_NOT_FOUND
        )

    def test_missing_settings_row_is_404(self):
        """A player who has never opened the settings screen has no CV."""
        self._user("nosettings", "No Settings")
        cache.clear()

        self.assertEqual(
            self._get(username="nosettings").status_code,
            status.HTTP_404_NOT_FOUND,
        )

    def test_private_profile_is_404_even_with_the_cv_enabled(self):
        """
        The documented deviation from spec §2.7, pinned. The two switches are
        NOT independent: the CV resolves through get_public_user, so a private
        profile has no CV however the CV's own toggle is set.
        """
        self.player.profile.is_public_profile = False
        self.player.profile.save(update_fields=["is_public_profile"])
        cache.clear()

        self.assertTrue(self.settings.is_enabled)
        self.assertEqual(
            self._get().status_code, status.HTTP_404_NOT_FOUND
        )

    def test_a_coach_is_404(self):
        coach = self._user("coachdev", "Coach Dev", role=User.Role.COACH)
        self._cv(coach)
        cache.clear()

        self.assertEqual(
            self._get(username="coachdev").status_code,
            status.HTTP_404_NOT_FOUND,
        )

    def test_a_scout_is_404(self):
        scout = self._user("scoutdev", "Scout Dev", role=User.Role.SCOUT)
        self._cv(scout)
        cache.clear()

        self.assertEqual(
            self._get(username="scoutdev").status_code,
            status.HTTP_404_NOT_FOUND,
        )

    def test_a_deactivated_player_is_404(self):
        gone = self._user("gone", "Gone Player", is_active=False)
        self._cv(gone)
        cache.clear()

        self.assertEqual(
            self._get(username="gone").status_code,
            status.HTTP_404_NOT_FOUND,
        )

    def test_unknown_username_is_404(self):
        self.assertEqual(
            self._get(username="nobody").status_code,
            status.HTTP_404_NOT_FOUND,
        )

    def test_the_four_refusals_are_byte_identical(self):
        """
        The one that actually matters. All four bodies must be the same bytes —
        a difference of a single word turns the 404 into an oracle.
        """
        self._set(is_enabled=False)

        hidden = self._user("hiddenplayer", "Hidden Player", is_public=False)
        self._cv(hidden)

        coach = self._user("coachtwo", "Coach Two", role=User.Role.COACH)
        self._cv(coach)
        cache.clear()

        bodies = []
        for username in ("riya", "hiddenplayer", "coachtwo", "whoisthis"):
            response = self._get(username=username)
            self.assertEqual(
                response.status_code, status.HTTP_404_NOT_FOUND, username
            )
            bodies.append(response.content)

        self.assertEqual(len(set(bodies)), 1, bodies)

    def test_the_404_leaks_no_name(self):
        hidden = self._user("hidden", "Hidden Player", is_public=False)
        self._cv(hidden)
        cache.clear()

        response = self._get(username="hidden")
        self.assertNotIn("Hidden Player", str(response.content))


# =====================================================================
# SECTION TOGGLES
# =====================================================================

class CVSectionToggleTests(CVTestCase):
    """
    Toggled off means ABSENT from the payload, never empty. The CV is
    server-rendered: an empty list still ships inside the page source, and
    "hidden" that a reader can View Source is not hidden.
    """

    def test_all_sections_are_present_by_default(self):
        data = self._data()

        for key in ("career", "achievements", "highlights"):
            self.assertIn(key, data)

    def test_career_off_removes_the_key(self):
        self._set(show_career=False)
        data = self._data()

        self.assertNotIn("career", data)
        self.assertIn("achievements", data)

    def test_achievements_off_removes_the_key(self):
        self._set(show_achievements=False)
        data = self._data()

        self.assertNotIn("achievements", data)
        self.assertIn("career", data)

    def test_highlights_off_removes_the_key(self):
        Highlight.objects.create(
            user=self.player, title="clip",
            file_url="https://cdn.test/c.mp4", public_id="h/c",
            visibility=Highlight.Visibility.EVERYONE,
        )
        self._set(show_highlights=False)

        data = self._data()

        self.assertNotIn("highlights", data)
        self.assertNotIn("https://cdn.test/c.mp4", str(data))

    def test_attributes_off_removes_them_from_the_sport_block(self):
        """
        The sport itself stays — a CV with no sport on it is not a CV. Only the
        player's own attribute values go.
        """
        self._set(show_attributes=False)
        data = self._data()

        primary_sport = data["profile"]["primary_sport"]
        self.assertEqual(primary_sport["sport"], "Football")
        self.assertNotIn("attributes", primary_sport)
        self.assertNotIn("Preferred foot", str(data))

    def test_attributes_on_carries_the_values(self):
        attributes = self._data()["profile"]["primary_sport"]["attributes"]

        self.assertEqual(
            [row["name"] for row in attributes], ["Preferred foot"]
        )

    def test_every_optional_section_off_leaves_only_the_always_keys(self):
        self._set(
            show_career=False,
            show_achievements=False,
            show_highlights=False,
        )

        self.assertEqual(set(self._data().keys()), CV_PAYLOAD_ALWAYS_KEYS)


# =====================================================================
# CONTACT
# =====================================================================

class CVContactTests(CVTestCase):

    def test_contact_is_absent_by_default(self):
        """show_contact defaults to False — safeguarding, not preference."""
        self.assertFalse(PlayerCVSettings().show_contact)
        self.assertNotIn("contact", self._data())

    def test_contact_off_leaks_no_phone_anywhere(self):
        self.assertNotIn("+919000000001", str(self._data()))

    def test_contact_on_exposes_the_phone(self):
        self._set(show_contact=True)
        data = self._data()

        self.assertEqual(data["contact"]["phone"], "+919000000001")

    def test_contact_carries_only_the_allow_listed_keys(self):
        self._set(show_contact=True)

        self.assertEqual(set(self._data()["contact"].keys()), CV_CONTACT_KEYS)

    def test_contact_never_carries_the_email(self):
        """
        Phone only, and there is no toggle that produces an email. The address
        is the account identifier — publishing it hands a scraper the first
        half of every credential-stuffing attempt against this login.
        """
        self._set(show_contact=True)
        data = self._data()

        self.assertNotIn("email", data["contact"])
        self.assertNotIn("riya@example.com", str(data))

    def test_contact_is_absent_when_the_player_has_no_phone(self):
        """Absent, not present-and-empty — an empty heading is worse than none."""
        nophone = self._user("nophone", "No Phone")
        self._cv(nophone, show_contact=True)
        cache.clear()

        self.assertNotIn("contact", self._data(username="nophone"))


# =====================================================================
# HIGHLIGHTS
# =====================================================================

class CVHighlightTests(CVTestCase):
    """
    The CV rail is `everyone`-only for EVERY viewer, unlike the profile bundle
    which resolves the real actor. A CV is printed, QR-scanned at a trial and
    forwarded on; a clip its owner restricted to recruiters must not turn into
    a public URL just because a recruiter is the one who opened the page.
    """

    def setUp(self):
        super().setUp()

        for visibility in Highlight.Visibility.values:
            Highlight.objects.create(
                user=self.player,
                title=visibility,
                file_url=f"https://cdn.test/{visibility}.mp4",
                public_id=f"h/{visibility}",
                visibility=visibility,
            )
        cache.clear()

    def _titles(self, **kwargs):
        return {h["title"] for h in self._data(**kwargs)["highlights"]}

    def test_only_everyone_clips_are_returned_anonymously(self):
        self.assertEqual(self._titles(), {Highlight.Visibility.EVERYONE})

    def test_a_signed_in_scout_still_sees_only_everyone_clips(self):
        """
        The one that would regress if somebody "fixed" the actor=None call in
        cv_payload to match the profile bundle.
        """
        scout = self._user("scoutviewer", "Scout Viewer", role=User.Role.SCOUT)
        self.client.force_authenticate(user=scout)

        self.assertEqual(self._titles(), {Highlight.Visibility.EVERYONE})

    def test_a_signed_in_scout_never_sees_a_recruiters_only_clip(self):
        scout = self._user("scouttwo", "Scout Two", role=User.Role.SCOUT)
        self.client.force_authenticate(user=scout)

        rendered = str(self._data())
        self.assertNotIn("recruiters_only.mp4", rendered)
        self.assertNotIn("followers_and_recruiters.mp4", rendered)

    def test_the_owner_sees_their_own_cv_as_everybody_else_does(self):
        """
        A preview that showed the owner more than the link actually carries
        would be a preview of the wrong page.
        """
        self.client.force_authenticate(user=self.player)

        self.assertEqual(self._titles(), {Highlight.Visibility.EVERYONE})

    def test_restricted_clips_are_absent_not_placeheld(self):
        """
        No "locked" tile. A marker would advertise that hidden footage exists,
        which is the one thing recruiters_only is meant to avoid.
        """
        highlights = self._data()["highlights"]

        self.assertEqual(len(highlights), 1)

    def test_owner_only_fields_are_stripped_from_the_rail(self):
        for clip in self._data()["highlights"]:
            self.assertNotIn("visibility", clip)
            self.assertNotIn("views_count", clip)


# =====================================================================
# VIEW COUNTER
# =====================================================================

class CVViewCounterTests(CVTestCase):

    def _count(self):
        self.settings.refresh_from_db(fields=["views_count"])
        return self.settings.views_count

    def test_an_anonymous_hit_counts(self):
        self._get(ip="10.0.0.5")

        self.assertEqual(self._count(), 1)

    def test_a_cache_hit_still_counts(self):
        """
        The reason the increment runs BEFORE the cache read. A shared CV is
        served from cache almost every time; counting after the early return
        would mean the counter silently stopped working the moment the feature
        started working.
        """
        self._get(ip="10.0.0.6")
        self.assertEqual(self._count(), 1)

        # Second reader, same cached bundle — a different IP so the per-IP
        # latch is not what is being measured here.
        self._get(ip="10.0.0.7")

        self.assertEqual(self._count(), 2)

    def test_the_same_ip_is_counted_once(self):
        """A refresh loop must not be able to inflate the number."""
        for _ in range(5):
            self._get(ip="10.0.0.8")

        self.assertEqual(self._count(), 1)

    def test_a_404_never_counts(self):
        self._set(is_enabled=False)
        self._get(ip="10.0.0.9")

        self.assertEqual(self._count(), 0)

    def test_the_counter_is_not_readable_as_someone_elses_business(self):
        """
        views_count IS on the public payload — it is an interest signal, like a
        post's view count, and the spec says a stale cached value is fine. What
        must not appear is anything about WHO looked.
        """
        data = self._data()

        self.assertIn("views_count", data)
        self.assertNotIn("viewers", data)


# =====================================================================
# CACHING
# =====================================================================

class CVCacheTests(CVTestCase):

    def test_a_toggle_invalidates_the_cached_cv_immediately(self):
        """
        Not left to the 60s TTL. A player who has just switched their phone
        number off must not watch it stay up for another minute.
        """
        self.client.force_authenticate(user=self.player)
        self.client.patch(
            CV_SETTINGS_URL, {"show_contact": True}, format="json"
        )
        self.client.force_authenticate(user=None)

        self.assertIn("contact", self._data(ip="10.0.1.1"))

        self.client.force_authenticate(user=self.player)
        self.client.patch(
            CV_SETTINGS_URL, {"show_contact": False}, format="json"
        )
        self.client.force_authenticate(user=None)

        self.assertNotIn("contact", self._data(ip="10.0.1.2"))

    def test_disabling_the_cv_takes_effect_immediately(self):
        self._data(ip="10.0.1.3")

        self.client.force_authenticate(user=self.player)
        self.client.patch(
            CV_SETTINGS_URL, {"is_enabled": False}, format="json"
        )
        self.client.force_authenticate(user=None)

        self.assertEqual(
            self._get(ip="10.0.1.4").status_code, status.HTTP_404_NOT_FOUND
        )

    def test_hiding_the_profile_clears_the_cached_cv_too(self):
        """
        The privacy toggle used to clear only the profile key. The CV is gated
        on the same flag, so a hidden profile would have kept serving a cached
        CV for up to a minute.
        """
        self._data(ip="10.0.1.5")

        self.client.force_authenticate(user=self.player)
        self.client.patch(
            USER_PRIVACY_URL, {"is_public_profile": False}, format="json"
        )
        self.client.force_authenticate(user=None)

        self.assertEqual(
            self._get(ip="10.0.1.6").status_code, status.HTTP_404_NOT_FOUND
        )
        self.assertEqual(
            self.client.get(PROFILE_URL.format("riya")).status_code,
            status.HTTP_404_NOT_FOUND,
        )


# =====================================================================
# OWNER SETTINGS
# =====================================================================

class CVSettingsEndpointTests(CVTestCase):

    def setUp(self):
        super().setUp()

        self.org = Organization.objects.create(
            name="Dream FC", username="dreamfc", type=Organization.Type.CLUB,
        )
        OrganizationProfile.objects.create(organization=self.org)

    def test_a_first_time_player_gets_defaults_not_a_404(self):
        fresh = self._user("fresh", "Fresh Player")
        self.client.force_authenticate(user=fresh)

        response = self.client.get(CV_SETTINGS_URL)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data["data"]
        self.assertFalse(data["is_enabled"])
        self.assertFalse(data["show_contact"])
        self.assertTrue(data["show_career"])
        self.assertTrue(
            PlayerCVSettings.objects.filter(user=fresh).exists()
        )

    def test_the_response_carries_the_profile_dependency(self):
        """
        So the settings screen can render "your link will not work until your
        profile is public" without a second fetch.
        """
        self.client.force_authenticate(user=self.player)
        response = self.client.get(CV_SETTINGS_URL)

        self.assertTrue(response.data["data"]["is_public_profile"])
        self.assertEqual(response.data["data"]["username"], "riya")

    def test_patch_updates_one_toggle_and_leaves_the_rest(self):
        self.client.force_authenticate(user=self.player)

        response = self.client.patch(
            CV_SETTINGS_URL, {"show_career": False}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.settings.refresh_from_db()
        self.assertFalse(self.settings.show_career)
        self.assertTrue(self.settings.show_achievements)
        self.assertTrue(self.settings.is_enabled)

    def test_patch_response_carries_the_dependency_too(self):
        self.player.profile.is_public_profile = False
        self.player.profile.save(update_fields=["is_public_profile"])

        self.client.force_authenticate(user=self.player)
        response = self.client.patch(
            CV_SETTINGS_URL, {"is_enabled": True}, format="json"
        )

        self.assertFalse(response.data["data"]["is_public_profile"])

    def test_an_empty_body_is_rejected(self):
        """Never a no-op that reports success."""
        self.client.force_authenticate(user=self.player)

        response = self.client.patch(CV_SETTINGS_URL, {}, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_coach_is_403(self):
        coach = self._user("coachx", "Coach X", role=User.Role.COACH)
        self.client.force_authenticate(user=coach)

        response = self.client.get(CV_SETTINGS_URL)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("player", response.data["message"].lower())

    def test_a_scout_cannot_patch(self):
        scout = self._user("scoutx", "Scout X", role=User.Role.SCOUT)
        self.client.force_authenticate(user=scout)

        response = self.client.patch(
            CV_SETTINGS_URL, {"is_enabled": True}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(PlayerCVSettings.objects.filter(user=scout).exists())

    def test_acting_as_an_organization_is_403_and_says_why(self):
        OrganizationMember.objects.create(
            organization=self.org, user=self.player,
            role=OrganizationMember.Role.OWNER,
        )
        self.client.force_authenticate(user=self.player)

        response = self.client.get(
            CV_SETTINGS_URL,
            HTTP_X_ACTOR_TYPE=TYPE_ORGANIZATION,
            HTTP_X_ACTOR_ID=str(self.org.id),
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("organization", response.data["message"].lower())

    def test_anonymous_cannot_read_or_write_settings(self):
        self.assertEqual(
            self.client.get(CV_SETTINGS_URL).status_code,
            status.HTTP_401_UNAUTHORIZED,
        )
        self.assertEqual(
            self.client.patch(
                CV_SETTINGS_URL, {"is_enabled": False}, format="json"
            ).status_code,
            status.HTTP_401_UNAUTHORIZED,
        )

    def test_the_settings_route_is_not_shadowed_by_a_username(self):
        """
        'user/cv/' is included ABOVE 'user/' in core/urls.py, because
        accounts.urls owns '<str:username>/details'. Without the ordering a
        player who grabbed the username "cv" would take the endpoint with them.
        """
        self.client.force_authenticate(user=self.player)

        self.assertEqual(
            self.client.get(CV_SETTINGS_URL).status_code, status.HTTP_200_OK
        )
