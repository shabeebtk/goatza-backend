"""The player's side: the invite link's page and join, and "my squads"."""

import json
from datetime import timedelta

from django.test import RequestFactory
from django.utils import timezone
from rest_framework.request import Request

from apps.organization.models import Organization
from apps.squads.models import Squad, SquadMember
from apps.squads.services.membership_service import MembershipService
from apps.squads.throttles import SquadInviteTokenThrottle, SquadJoinThrottle
from apps.squads.tests.base import SquadTestBase, make_user

Status = SquadMember.Status


class InvitePageTests(SquadTestBase):

    def setUp(self):
        super().setUp()
        self.path = f"/squads/invite/{self.squad.invite_token}"
        self.squad.staff.create(member=self.coach_member, role="head_coach")
        self.activate(self.squad, self.p2, jersey_number=9)

    def test_anonymous(self):
        response = self.anon_get(self.path)

        self.assertEqual(response.status_code, 200, response.data)
        data = response.data["data"]
        self.assertEqual(data["squad"]["name"], "U17 Boys")
        self.assertEqual(data["squad"]["members_count"], 1)
        self.assertEqual(data["organization"]["username"], "dreamfc")
        self.assertEqual(data["head_coach"], {"name": "Cora Coach"})
        self.assertIsNone(data["me"])

        # Never a player's name or photo.
        text = json.dumps(data, default=str)
        self.assertNotIn("Bilal", text)
        self.assertNotIn("bilal", text)

    def test_the_head_coach_follows_the_staff_privacy_rules(self):
        from apps.accounts.models import User, UserProfile

        UserProfile.objects.filter(user=self.coach).update(is_public_profile=False)
        self.assertIsNone(self.anon_get(self.path).data["data"]["head_coach"])
        self.assertEqual(
            self.user_get(self.p1, f"invite/{self.squad.invite_token}").data["data"]["head_coach"],
            {"name": "Cora Coach"},
        )

        UserProfile.objects.filter(user=self.coach).update(is_public_profile=True)
        User.objects.filter(id=self.coach.id).update(is_active=False)
        self.assertIsNone(self.anon_get(self.path).data["data"]["head_coach"])
        self.assertIsNone(
            self.user_get(self.p1, f"invite/{self.squad.invite_token}").data["data"]["head_coach"]
        )

    def test_me(self):
        me = self.user_get(self.p1, f"invite/{self.squad.invite_token}").data["data"]["me"]
        self.assertEqual(me, {"state": "none", "member_id": None, "can_join": True, "reason": None})

        row, _ = MembershipService.request_join(self.p1, self.squad)
        me = self.user_get(self.p1, f"invite/{self.squad.invite_token}").data["data"]["me"]
        self.assertEqual((me["state"], me["member_id"], me["can_join"]), ("pending", str(row.id), False))

        me = self.user_get(self.p2, f"invite/{self.squad.invite_token}").data["data"]["me"]
        self.assertEqual((me["state"], me["can_join"]), ("active", False))

        MembershipService.invite(self.owner_actor(), self.squad.id, self.p3.id)
        me = self.user_get(self.p3, f"invite/{self.squad.invite_token}").data["data"]["me"]
        self.assertEqual((me["state"], me["can_join"]), ("invited", True))

        me = self.user_get(self.coach, f"invite/{self.squad.invite_token}").data["data"]["me"]
        self.assertEqual((me["can_join"], me["reason"]), (False, "not_a_player"))

        self.client.force_authenticate(user=self.p1)
        response = self.client.get(
            self.path,
            HTTP_X_ACTOR_TYPE="organization", HTTP_X_ACTOR_ID=str(self.org.id),
        )
        # p1 is not a member of the org: resolve_actor refuses before anything.
        self.assertEqual(response.status_code, 403)

        response = self.client.get(self.path, **self._org_headers(self.owner))
        me = response.data["data"]["me"]
        self.assertEqual(me["reason"], "not_a_player")

    def test_a_player_acting_as_their_club_is_told_to_switch(self):
        self._member(self.p1, "staff")
        response = self.client.get(self.path, **self._org_headers(self.p1))
        self.assertEqual(response.data["data"]["me"]["reason"], "switch_to_personal")

    def test_one_404_for_every_miss(self):
        token = self.squad.invite_token

        self.assert_error(self.anon_get("/squads/invite/not-a-token"), "not_found", 404)

        Squad.objects.filter(id=self.squad.id).update(invite_enabled=False)
        self.assert_error(self.anon_get(self.path), "not_found", 404)

        Squad.objects.filter(id=self.squad.id).update(invite_enabled=True, is_deleted=True)
        self.assert_error(self.anon_get(self.path), "not_found", 404)

        Squad.objects.filter(id=self.squad.id).update(is_deleted=False)
        Organization.objects.filter(id=self.org.id).update(is_suspended=True)
        self.assert_error(self.anon_get(self.path), "not_found", 404)

        Organization.objects.filter(id=self.org.id).update(is_suspended=False)
        self.assertEqual(self.anon_get(self.path).status_code, 200)

        # A block, either way, is the same 404 for the signed-in person.
        self.block(self.org, target_user=self.p1)
        self.assert_error(self.user_get(self.p1, f"invite/{token}"), "not_found", 404)
        self.block(self.p3, target_org=self.org)
        self.assert_error(self.user_get(self.p3, f"invite/{token}"), "not_found", 404)

    def test_a_reset_link_is_dead(self):
        old = self.squad.invite_token
        self.org_post(self.owner, f"{self.squad.id}/invite-link/reset")
        self.assert_error(self.anon_get(f"/squads/invite/{old}"), "not_found", 404)


class JoinTests(SquadTestBase):

    def setUp(self):
        super().setUp()
        self.path = f"invite/{self.squad.invite_token}/join"

    def test_join(self):
        response = self.user_post(self.p1, self.path)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["data"]["state"], "pending")

        again = self.user_post(self.p1, self.path)
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.data["data"]["member_id"], response.data["data"]["member_id"])

    def test_an_open_invite_becomes_active(self):
        MembershipService.invite(self.owner_actor(), self.squad.id, self.p1.id)
        response = self.user_post(self.p1, self.path)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["state"], "active")

    def test_refusals(self):
        self.assert_error(self.user_post(self.coach, self.path), "not_a_player")

        self._member(self.p1, "staff")
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                f"/squads/{self.path}", {}, format="json", **self._org_headers(self.p1)
            )
        self.assert_error(response, "switch_to_personal", 403)

        Squad.objects.filter(id=self.squad.id).update(invite_enabled=False)
        self.assert_error(self.user_post(self.p2, self.path), "not_found", 404)

        Squad.objects.filter(id=self.squad.id).update(invite_enabled=True)
        self.block(self.org, target_user=self.p2)
        self.assert_error(self.user_post(self.p2, self.path), "not_found", 404)

    def test_throttle_keys(self):
        factory = RequestFactory()
        raw = factory.post("/squads/invite/x/join", REMOTE_ADDR="10.0.0.7")
        raw.user = self.p1
        request = Request(raw)
        request.user = self.p1

        self.assertEqual(
            SquadInviteTokenThrottle().get_cache_key(request, None),
            "throttle_squad_invite_token_10.0.0.7",
        )
        self.assertEqual(
            SquadJoinThrottle().get_cache_key(request, None),
            f"throttle_squad_join_user:{self.p1.pk}",
        )


class MySquadsTests(SquadTestBase):

    def test_mine(self):
        seniors = self.make_squad("Seniors")
        thirds = self.make_squad("Thirds")
        expired = self.make_squad("Old")
        self.activate(self.squad, self.p1, jersey_number=8, notes="private")
        MembershipService.invite(self.owner_actor(), seniors.id, self.p1.id)
        MembershipService.request_join(self.p1, thirds)
        SquadMember.objects.create(
            squad=expired, user=self.p1, status=Status.INVITED,
            source=SquadMember.Source.INVITE,
            expires_at=timezone.now() - timedelta(days=1),
        )

        items = self.user_get(self.p1, "mine").data["data"]["items"]

        self.assertEqual(
            [(item["status"], item["squad"]["name"]) for item in items],
            [("active", "U17 Boys"), ("invited", "Seniors"), ("pending", "Thirds")],
        )
        self.assertEqual(items[0]["jersey_number"], 8)
        self.assertEqual(items[0]["organization"]["username"], "dreamfc")
        self.assertNotIn("notes", items[0])

    def test_mine_is_the_persons(self):
        self.assert_error(
            self.client.get("/squads/mine", **self._org_headers(self.owner)),
            "switch_to_personal", 403,
        )

    def test_answers(self):
        invite = MembershipService.invite(self.owner_actor(), self.squad.id, self.p1.id)

        # Somebody else's row is closed to them, whatever it is.
        self.assert_error(self.user_post(self.p2, f"mine/{invite.id}/accept"), "invite_closed")

        response = self.user_post(self.p1, f"mine/{invite.id}/accept")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["status"], "active")

        response = self.user_post(self.p1, f"mine/{invite.id}/leave")
        self.assertEqual(response.status_code, 200, response.data)
        invite.refresh_from_db()
        self.assertEqual(invite.status, Status.LEFT)

        request, _ = MembershipService.request_join(self.p2, self.squad)
        self.assert_error(self.user_post(self.p1, f"mine/{request.id}/withdraw"), "not_pending")
        self.assertEqual(self.user_post(self.p2, f"mine/{request.id}/withdraw").status_code, 200)
        self.assertFalse(SquadMember.objects.filter(id=request.id).exists())
        self.squad.refresh_from_db()
        self.assertEqual(self.squad.pending_count, 0)

        invite = MembershipService.invite(self.owner_actor(), self.squad.id, self.p3.id)
        self.assertEqual(self.user_post(self.p3, f"mine/{invite.id}/decline").status_code, 200)
        self.assertFalse(SquadMember.objects.filter(id=invite.id).exists())
