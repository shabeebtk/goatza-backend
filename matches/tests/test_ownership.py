"""
Who may touch a diary at all.

Two separate invariants live here and they fail differently. "You are not a
player" is a 403 that names the reason, because a coach looking at a screen
needs to know it is not for them. "That is not your match" is a 404 that names
nothing, because a diary is private and a 403 would confirm the row exists.
"""

from datetime import timedelta

from rest_framework import status
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from matches.models import MatchDiarySettings, MatchEntry
from matches.services.diary_settings_services import DiarySettingsService
from matches.services.match_services import MatchService
from matches.tests.base import MatchDiaryTestCase


class NonPlayerRolesAreRefusedTests(MatchDiaryTestCase):
    """
    The diary is a player's own record. A coach and a scout have profiles, not
    diaries, and every write must say so rather than quietly accepting one.
    """

    def setUp(self):
        super().setUp()
        self.match = self._played()

    def _assert_all_writes_refused(self, user):
        create = self._create(user=user)
        self.assertEqual(create.status_code, status.HTTP_403_FORBIDDEN, user.role)

        update = self._update(self.match.id, {"notes": "mine now"}, user=user)
        self.assertEqual(update.status_code, status.HTTP_403_FORBIDDEN, user.role)

        delete = self._delete(self.match.id, user=user)
        self.assertEqual(delete.status_code, status.HTTP_403_FORBIDDEN, user.role)

        settings = self._patch_settings({"showcase_summary": True}, user=user)
        self.assertEqual(settings.status_code, status.HTTP_403_FORBIDDEN, user.role)

    def test_a_coach_is_refused_on_every_write(self):
        self._assert_all_writes_refused(self.coach)

    def test_a_scout_is_refused_on_every_write(self):
        self._assert_all_writes_refused(self.scout)

    def test_the_refusal_names_the_role_rather_than_saying_forbidden(self):
        resp = self._create(user=self.coach)

        self.assertIn("coach", resp.data["message"].lower())
        self.assertFalse(resp.data["success"])

    def test_a_refused_write_leaves_nothing_behind(self):
        self._create(user=self.coach, body={"opponent_name": "Ghost FC"})

        self.assertFalse(
            MatchEntry.objects.filter(opponent_name="Ghost FC").exists()
        )

    def test_role_is_checked_before_the_body_is_validated(self):
        """
        A coach sending nonsense gets 403, not a critique of a body they were
        never allowed to send.
        """
        resp = self._create(user=self.coach, body={"sport": "not-a-uuid"})

        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)


class OrganizationActorIsRefusedTests(MatchDiaryTestCase):
    """
    A diary belongs to a person. The same human acting through one of their
    organizations is not logging their own matches, and the actor is the only
    thing that knows which it was.
    """

    def setUp(self):
        super().setUp()
        self.match = self._played()

    def test_an_org_actor_is_refused_on_every_write(self):
        create = self._create(user=self.orguser, org=self.org)
        self.assertEqual(create.status_code, status.HTTP_403_FORBIDDEN)

        update = self._update(
            self.match.id, {"notes": "x"}, user=self.orguser, org=self.org
        )
        self.assertEqual(update.status_code, status.HTTP_403_FORBIDDEN)

        delete = self._delete(self.match.id, user=self.orguser, org=self.org)
        self.assertEqual(delete.status_code, status.HTTP_403_FORBIDDEN)

        settings = self._patch_settings(
            {"showcase_summary": True}, user=self.orguser, org=self.org
        )
        self.assertEqual(settings.status_code, status.HTTP_403_FORBIDDEN)

    def test_a_player_acting_as_their_own_org_is_still_refused(self):
        """
        Membership does not make it personal. The refusal is about the actor,
        not about whether this human happens to own the club.
        """
        from organization.models import OrganizationMember

        OrganizationMember.objects.create(
            organization=self.org,
            user=self.player,
            role=OrganizationMember.Role.STAFF,
        )

        resp = self._create(user=self.player, org=self.org)

        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_the_refusal_points_at_the_personal_account(self):
        resp = self._create(user=self.orguser, org=self.org)

        self.assertIn("personal account", resp.data["message"])


class AnotherPlayersMatchIsInvisibleTests(MatchDiaryTestCase):
    """
    404, never 403.

    A missing match, a deleted one and one belonging to somebody else are
    deliberately indistinguishable. Answering 403 for the third would tell
    whoever is walking ids that this one is real — exactly what they were
    trying to find out.
    """

    def setUp(self):
        super().setUp()
        self.theirs = self._played(actor=self.other_actor, opponent_name="Private FC")

    def test_another_players_match_does_not_appear_in_the_list(self):
        resp = self._list()

        self.assertEqual(resp.data["data"]["count"], 0)
        self.assertEqual(self._results(resp), [])

    def test_updating_another_players_match_is_404_not_403(self):
        resp = self._update(self.theirs.id, {"notes": "mine now"})

        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_deleting_another_players_match_is_404_not_403(self):
        resp = self._delete(self.theirs.id)

        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)
        self.theirs.refresh_from_db()
        self.assertFalse(self.theirs.is_deleted)

    def test_a_foreign_match_is_indistinguishable_from_one_that_never_existed(self):
        foreign = self._delete(self.theirs.id)
        ghost = self._delete("0198f000-0000-7000-8000-000000000000")

        self.assertEqual(foreign.status_code, ghost.status_code)
        self.assertEqual(foreign.data["message"], ghost.data["message"])

    def test_the_service_raises_notfound_rather_than_permissiondenied(self):
        with self.assertRaises(NotFound):
            MatchService.update_match(self.actor, self.theirs.id, notes="x")

    def test_a_soft_deleted_match_reads_as_gone_to_its_own_owner(self):
        mine = self._played()
        MatchService.delete_match(self.actor, mine.id)

        resp = self._update(mine.id, {"notes": "back please"})

        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)


class CareerEntryOwnershipTests(MatchDiaryTestCase):
    """
    A match may be attached to a career stint, and the stint must be the
    player's own — otherwise anyone could hang their matches off somebody
    else's club spell by guessing an id.
    """

    def setUp(self):
        super().setUp()
        self.mine = self._career_entry(user=self.player, organization_name="My FC")
        self.theirs = self._career_entry(user=self.other, organization_name="Their FC")

    def test_a_player_can_attach_their_own_career_entry(self):
        resp = self._create(body={"career_entry": str(self.mine.id)})

        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(
            resp.data["data"]["career_entry"]["organization_name"], "My FC"
        )

    def test_a_player_cannot_attach_another_players_career_entry(self):
        resp = self._create(body={"career_entry": str(self.theirs.id)})

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(MatchEntry.objects.count(), 0)

    def test_a_foreign_career_entry_cannot_be_attached_by_update_either(self):
        match = self._played()

        resp = self._update(match.id, {"career_entry": str(self.theirs.id)})

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        match.refresh_from_db()
        self.assertIsNone(match.career_entry_id)

    def test_the_refusal_does_not_confirm_whose_entry_it_is(self):
        with self.assertRaises(ValidationError) as caught:
            MatchService.create_match(
                self.actor,
                sport=self.football.id,
                date=self.today,
                career_entry=self.theirs.id,
            )

        self.assertIn("not one of yours", str(caught.exception.detail))


class SettingsAreScopedToTheRequesterTests(MatchDiaryTestCase):
    """
    There is no id in the settings URL, so the only row anybody can reach is
    their own. This proves it stays that way when two players are involved.
    """

    def test_each_player_gets_their_own_row(self):
        self._get_settings(user=self.player)
        self._get_settings(user=self.other)

        self.assertEqual(MatchDiarySettings.objects.count(), 2)
        self.assertEqual(
            set(MatchDiarySettings.objects.values_list("user_id", flat=True)),
            {self.player.id, self.other.id},
        )

    def test_one_players_toggle_does_not_move_anothers(self):
        self._patch_settings({"showcase_summary": True}, user=self.player)

        resp = self._get_settings(user=self.other)

        self.assertFalse(resp.data["data"]["showcase_summary"])

    def test_streaks_are_per_player(self):
        self._played(actor=self.actor, date=self.monday)
        self._played(actor=self.other_actor, date=self.monday)
        self._played(actor=self.other_actor, date=self.monday - timedelta(weeks=1))

        mine = self._get_settings(user=self.player).data["data"]
        theirs = self._get_settings(user=self.other).data["data"]

        self.assertEqual(mine["current_streak_weeks"], 1)
        self.assertEqual(theirs["current_streak_weeks"], 2)

    def test_settings_service_refuses_an_org_actor(self):
        with self.assertRaises(PermissionDenied):
            DiarySettingsService.get_settings(self.org_actor)
