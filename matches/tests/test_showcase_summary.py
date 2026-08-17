"""
The showcased summary — one player's totals as somebody else reads them.

Two invariants live here. The first is that ``showcase_summary`` actually gates
something, because a toggle wired to nothing is worse than no toggle. The
second is that every refusal is the SAME refusal: a distinct "showcase is off"
response would let anybody enumerate which players keep a diary, which is
precisely the thing the toggle was meant to control.

The upload-type test sits at the bottom rather than in its own file: it is the
other half of the same photo feature, and it is one rule.
"""

from datetime import timedelta

from rest_framework import status

from matches.models import MatchDiarySettings
from matches.tests.base import MatchDiaryTestCase


def showcase_url(username):
    return f"/matches/summary/{username}"


UPLOAD_URL = "/user/get/upload/signature"


class ShowcaseGateTests(MatchDiaryTestCase):
    """
    Who may read whose summary. Every refusal is 404 with the same message —
    the visitor learns nothing about why.
    """

    def setUp(self):
        super().setUp()
        self._played(
            date=self.monday,
            result="win",
            minutes_played=90,
            self_rating=4,
            stats=[{"stat_field": self.goals.id, "value": 2}],
        )

    def _showcase_on(self, user=None):
        settings = MatchDiarySettings.objects.get(user=user or self.player)
        settings.showcase_summary = True
        settings.save(update_fields=["showcase_summary"])
        return settings

    def _get(self, username, viewer):
        headers = self._auth(viewer)
        return self.client.get(showcase_url(username), **headers)

    def test_showcase_on_returns_the_summary_to_another_player(self):
        self._showcase_on()

        resp = self._get(self.player.username, self.other)

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.data["data"]
        self.assertEqual(data["total_matches"], 1)
        self.assertEqual(data["wins"], 1)
        self.assertEqual(data["minutes_total"], 90)
        self.assertEqual(data["username"], self.player.username)
        self.assertFalse(data["is_owner"])

    def test_showcase_off_is_404(self):
        resp = self._get(self.player.username, self.other)

        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_a_coach_username_is_404(self):
        resp = self._get(self.coach.username, self.other)

        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_an_unknown_username_is_404(self):
        resp = self._get("ghost", self.other)

        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_a_deactivated_player_is_404_even_with_showcase_on(self):
        self._showcase_on()
        self.player.is_active = False
        self.player.save(update_fields=["is_active"])

        resp = self._get(self.player.username, self.other)

        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_every_refusal_is_indistinguishable_from_every_other(self):
        """
        The enumeration guard. If "showcase off" answered differently from
        "no such user", the endpoint would be a directory of who keeps a diary.
        """
        off = self._get(self.player.username, self.other)
        coach = self._get(self.coach.username, self.other)
        ghost = self._get("ghost", self.other)

        self.assertEqual(off.status_code, coach.status_code)
        self.assertEqual(coach.status_code, ghost.status_code)
        self.assertEqual(off.data["message"], coach.data["message"])
        self.assertEqual(coach.data["message"], ghost.data["message"])

    def test_it_still_requires_authentication(self):
        self._showcase_on()
        self.client.force_authenticate(user=None)

        resp = self.client.get(showcase_url(self.player.username))

        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_a_coach_may_read_a_showcased_summary(self):
        """
        The refusal is about whose diary it is, not about who is asking — a
        visiting coach is the whole point of the feature.
        """
        self._showcase_on()

        resp = self._get(self.player.username, self.coach)

        self.assertEqual(resp.status_code, status.HTTP_200_OK)


class OwnerPreviewTests(MatchDiaryTestCase):
    """
    The owner reads their own regardless of the toggle. Previewing what a coach
    would see must not require publishing it first.
    """

    def setUp(self):
        super().setUp()
        self._played(date=self.monday, result="win", minutes_played=90)

    def _get(self, username, viewer):
        headers = self._auth(viewer)
        return self.client.get(showcase_url(username), **headers)

    def test_the_owner_gets_200_with_the_showcase_off(self):
        resp = self._get(self.player.username, self.player)

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertTrue(resp.data["data"]["is_owner"])
        self.assertEqual(resp.data["data"]["total_matches"], 1)

    def test_a_player_who_has_never_opened_the_diary_can_still_preview(self):
        MatchDiarySettings.objects.all().delete()

        resp = self._get(self.player.username, self.player)

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["data"]["current_streak_weeks"], 0)

    def test_previewing_does_not_create_a_settings_row(self):
        """
        Looking at your own profile is a read. The lazy create belongs to the
        settings screen, not here.
        """
        MatchDiarySettings.objects.all().delete()

        self._get(self.player.username, self.player)

        self.assertEqual(MatchDiarySettings.objects.count(), 0)


class ShowcasePayloadTests(MatchDiaryTestCase):
    """
    What the visitor actually receives: the aggregates plus the streak, and
    nothing that belongs to an individual match.
    """

    def setUp(self):
        super().setUp()
        for weeks_back in (0, 1, 2):
            self._played(
                date=self.monday - timedelta(weeks=weeks_back),
                result="win",
                minutes_played=90,
                self_rating=4,
                opponent_name="Private opponent",
                notes="a private note",
                stats=[{"stat_field": self.goals.id, "value": 1}],
            )

        settings = MatchDiarySettings.objects.get(user=self.player)
        settings.showcase_summary = True
        settings.save(update_fields=["showcase_summary"])

    def _visitor_payload(self):
        headers = self._auth(self.other)
        return self.client.get(
            showcase_url(self.player.username), **headers
        ).data["data"]

    def test_the_streak_rides_along_with_the_totals(self):
        data = self._visitor_payload()

        self.assertEqual(data["current_streak_weeks"], 3)
        self.assertEqual(data["longest_streak_weeks"], 3)

    def test_the_stats_block_is_the_same_one_the_owner_reads(self):
        visitor = self._visitor_payload()
        owner = self._summary(user=self.player).data["data"]

        self.assertEqual(visitor["stats"], owner["stats"])
        self.assertEqual(visitor["form"], owner["form"])
        self.assertEqual(visitor["average_rating"], owner["average_rating"])

    def test_no_individual_match_detail_leaks_into_the_payload(self):
        body = str(self._visitor_payload())

        self.assertNotIn("Private opponent", body)
        self.assertNotIn("a private note", body)

    def test_the_year_and_sport_filters_work_for_a_visitor_too(self):
        self._played(
            date=self.last_season,
            sport=self.cricket.id,
            result="loss",
        )

        headers = self._auth(self.other)
        resp = self.client.get(
            f"{showcase_url(self.player.username)}?year={self.last_season.year}",
            **headers,
        )

        self.assertEqual(resp.data["data"]["total_matches"], 1)
        self.assertEqual(resp.data["data"]["losses"], 1)

    def test_a_junk_filter_is_a_400_not_a_500(self):
        headers = self._auth(self.other)
        resp = self.client.get(
            f"{showcase_url(self.player.username)}?year=banana", **headers
        )

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)


class MatchPhotoUploadTypeTests(MatchDiaryTestCase):
    """
    The signed-upload slot for a match photo.

    User-only, like achievements and for the same reason: a match belongs to
    the person who played it, and there is no org-side diary to upload for.
    """

    def _config(self, user, org=None, upload_type="matches"):
        headers = self._auth(user, org)
        return self.client.get(f"{UPLOAD_URL}?type={upload_type}", **headers)

    def test_a_player_gets_a_signed_upload_scoped_to_their_own_folder(self):
        resp = self._config(self.player)

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        upload = resp.data["data"]["uploads"][0]
        self.assertEqual(upload["folder"], f"users/{self.player.id}/matches")

    def test_an_organization_actor_is_refused(self):
        resp = self._config(self.orguser, org=self.org)

        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("personal account", resp.data["message"])

    def test_the_folder_does_not_collide_with_achievements(self):
        matches = self._config(self.player).data["data"]["uploads"][0]
        achievements = self._config(
            self.player, upload_type="achievements"
        ).data["data"]["uploads"][0]

        self.assertNotEqual(matches["folder"], achievements["folder"])
        self.assertTrue(matches["folder"].endswith("/matches"))
        self.assertTrue(achievements["folder"].endswith("/achievements"))
