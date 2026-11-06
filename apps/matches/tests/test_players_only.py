"""
The match diary is a player's own, acting as themselves. No data is deleted: a
person whose role changes keeps their rows, they just cannot reach them from here.
"""

from rest_framework import status

from apps.accounts.views.user_upload_signature_views import resolve_upload_actor
from apps.matches.models import MatchEntry
from core.actor import Actor

from .base import STAT_FIELDS_URL, SUMMARY_URL, MatchDiaryTestCase


class PlayersOnlyTests(MatchDiaryTestCase):
    def _refused(self):
        """Everyone who must be turned away, as (label, user, org header)."""
        return (
            ("coach", self.coach, None),
            ("scout", self.scout, None),
            ("org_user", self.orguser, None),
            ("acting as an org", self.orguser, self.org),
        )

    def _assert_all_403(self, call):
        for label, user, org in self._refused():
            with self.subTest(label):
                response = call(user, org)
                self.assertEqual(
                    response.status_code, status.HTTP_403_FORBIDDEN, response.data
                )

    def test_list(self):
        self._assert_all_403(lambda user, org: self._list(user=user, org=org))

    def test_upcoming(self):
        self._assert_all_403(lambda user, org: self._upcoming(user=user, org=org))

    def test_summary(self):
        self._assert_all_403(lambda user, org: self._summary(user=user, org=org))

    def test_create(self):
        self._assert_all_403(lambda user, org: self._create(user=user, org=org, body={}))

    def test_update(self):
        match = self._played()

        self._assert_all_403(
            lambda user, org: self._update(match.id, {"notes": "x"}, user=user, org=org)
        )

    def test_delete(self):
        match = self._played()

        self._assert_all_403(lambda user, org: self._delete(match.id, user=user, org=org))
        self.assertTrue(MatchEntry.objects.filter(id=match.id, is_deleted=False).exists())

    def test_settings_read_and_write(self):
        self._assert_all_403(lambda user, org: self._get_settings(user=user, org=org))
        self._assert_all_403(
            lambda user, org: self._patch_settings(
                {"showcase_summary": True}, user=user, org=org
            )
        )

    def test_stat_fields(self):
        self._assert_all_403(
            lambda user, org: self.client.get(
                STAT_FIELDS_URL,
                {"sport_id": str(self.football.id)},
                **self._auth(user, org),
            )
        )

    def test_a_player_gets_every_read(self):
        for call in (self._list, self._upcoming, self._summary, self._get_settings):
            with self.subTest(call.__name__):
                self.assertEqual(call(user=self.player).status_code, status.HTTP_200_OK)

    def test_a_players_rows_survive_a_role_change(self):
        match = self._played()

        self.player.role = "coach"
        self.player.save(update_fields=["role"])

        self.assertEqual(self._list(user=self.player).status_code, 403)
        self.assertTrue(MatchEntry.objects.filter(id=match.id, is_deleted=False).exists())


class MatchPhotoTests(MatchDiaryTestCase):
    """The signed upload for a match photo is part of the diary, so it has the gate."""

    def _resolve(self, actor):
        return resolve_upload_actor(None, actor, "matches", None)

    def test_a_player_gets_one(self):
        actor = Actor(actor_type="user", user=self.player)

        self.assertEqual(self._resolve(actor), (actor, None))

    def test_a_person_who_is_not_a_player_is_refused(self):
        for user in (self.coach, self.scout, self.orguser):
            with self.subTest(user.username):
                resolved, refusal = self._resolve(Actor(actor_type="user", user=user))

                self.assertIsNone(resolved)
                self.assertEqual(refusal[1:], ("type", 403))
                self.assertIn("match diary", refusal[0])

    def test_an_organization_is_refused(self):
        actor = Actor(actor_type="organization", organization=self.org)

        resolved, refusal = self._resolve(actor)

        self.assertIsNone(resolved)
        self.assertEqual(refusal[2], 403)

    def test_other_personal_uploads_do_not_get_this_rule(self):
        actor = Actor(actor_type="user", user=self.coach)

        self.assertEqual(
            resolve_upload_actor(None, actor, "achievements", None), (actor, None)
        )


class PublicSummaryTests(MatchDiaryTestCase):
    def _showcase(self, username, viewer):
        return self.client.get(f"{SUMMARY_URL}/{username}", **self._auth(viewer))

    def test_a_non_players_summary_is_404_for_anyone(self):
        for target in (self.coach, self.scout, self.orguser):
            for viewer in (self.player, target):
                with self.subTest(target=target.username, viewer=viewer.username):
                    response = self._showcase(target.username, viewer)

                    self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_the_same_404_as_a_typo(self):
        coach = self._showcase(self.coach.username, self.player)
        typo = self._showcase("no-such-person", self.player)

        self.assertEqual(coach.data["message"], typo.data["message"])

    def test_a_player_previews_their_own(self):
        response = self._showcase(self.player.username, self.player)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
