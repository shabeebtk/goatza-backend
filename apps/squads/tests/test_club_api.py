"""The club's endpoints: who may do what, and the squad itself."""

import uuid
from datetime import timedelta

from django.utils import timezone

from apps.careers.models import CareerEntry
from apps.organization.models import Organization, OrganizationMember, OrganizationProfile
from apps.squads.models import Squad, SquadMember, SquadStaff
from apps.squads.services.membership_service import MembershipService
from apps.squads.throttles import SquadDirectInviteThrottle
from apps.squads.tests.base import MEDIA, SquadTestBase, make_user

Status = SquadMember.Status


class PermissionTests(SquadTestBase):

    def test_a_coach_runs_players_but_cannot_change_squads(self):
        sid = self.squad.id

        for method, path, body in (
            ("post", "create", {"name": "Seniors", "sport_id": str(self.football.id)}),
            ("patch", f"{sid}/update", {"name": "Renamed"}),
            ("post", f"{sid}/delete", {}),
            ("post", f"{sid}/photo", {"remove": True}),
            ("patch", f"{sid}/invite-link", {"enabled": False}),
            ("post", f"{sid}/invite-link/reset", {}),
            ("post", f"{sid}/staff", {"member_id": str(self.coach_member.id), "role": "head_coach"}),
        ):
            call = self.org_post if method == "post" else self.org_patch
            self.assert_error(call(self.coach, path, body), "forbidden", 403)

        self.assertEqual(self.org_get(self.coach, "list").status_code, 200)
        detail = self.org_get(self.coach, f"{sid}").data["data"]
        self.assertIsNotNone(detail["invite"])
        self.assertEqual(self.org_get(self.coach, f"{sid}/requests").status_code, 200)

        response = self.org_post(
            self.coach, f"{sid}/invites", {"user_id": str(self.p1.id)}
        )
        self.assertEqual(response.status_code, 201, response.data)

    def test_staff_can_only_read(self):
        sid = self.squad.id
        self.activate(self.squad, self.p1, notes="Needs work on his left foot")

        self.assertEqual(self.org_get(self.staff, "list").status_code, 200)
        detail = self.org_get(self.staff, f"{sid}").data["data"]
        self.assertIsNone(detail["invite"])

        roster = self.org_get(self.staff, f"{sid}/members").data["data"]
        member = roster["groups"][0]["members"][0]
        self.assertNotIn("notes", member)

        self.assert_error(self.org_get(self.staff, f"{sid}/requests"), "forbidden", 403)
        self.assert_error(
            self.org_post(self.staff, f"{sid}/invites", {"user_id": str(self.p2.id)}),
            "forbidden", 403,
        )
        self.assert_error(
            self.org_post(self.staff, f"{sid}/requests/approve", {"items": []}),
            "forbidden", 403,
        )

    def test_a_reviewer_sees_the_notes(self):
        self.activate(self.squad, self.p1, notes="Needs work on his left foot")
        roster = self.org_get(self.coach, f"{self.squad.id}/members").data["data"]
        self.assertEqual(
            roster["groups"][0]["members"][0]["notes"], "Needs work on his left foot"
        )

    def test_someone_outside_the_org_is_refused(self):
        outsider = make_user("rival", role="coach")
        rival = Organization.objects.create(name="Rival FC", username="rivalfc", type="club")
        OrganizationProfile.objects.create(organization=rival)
        self._member(outsider, OrganizationMember.Role.OWNER, org=rival)

        # Naming our org in the header: not a member.
        response = self.org_get(outsider, "list")
        self.assert_error(response, "forbidden", 403)

        # As their own org: our squad is simply not there.
        response = self.org_get(outsider, f"{self.squad.id}", org=rival)
        self.assert_error(response, "not_found", 404)

    def test_a_person_acting_as_themselves_is_told_to_switch(self):
        response = self.user_get(self.owner, "list")
        self.assert_error(response, "switch_to_org", 403)

    def test_the_invite_throttle_draws_from_the_org_bucket_only_for_reviewers(self):
        throttle = SquadDirectInviteThrottle()
        org_id = str(self.org.id)

        self.assertEqual(throttle.org_ident(self.owner, org_id), f"org:{org_id}")
        self.assertEqual(throttle.org_ident(self.coach, org_id), f"org:{org_id}")
        self.assertEqual(
            throttle.org_ident(self.staff, org_id), f"{self.staff.pk}:org:{org_id}"
        )
        self.assertEqual(
            throttle.org_ident(self.p1, org_id), f"{self.p1.pk}:org:{org_id}"
        )


class CreateUpdateTests(SquadTestBase):

    def test_create(self):
        response = self.org_post(self.owner, "create", {
            "name": "  Senior Men ", "sport_id": str(self.football.id),
            "level": "senior", "season": "2026–27", "description": "First team.",
        })

        self.assertEqual(response.status_code, 201, response.data)
        data = response.data["data"]
        self.assertEqual(data["name"], "Senior Men")
        self.assertEqual(data["level_label"], "Senior")
        self.assertEqual(data["sport"]["name"], "Football")
        self.assertEqual(data["config"]["group_order"][0], "Goalkeepers")
        self.assertTrue(data["invite"]["url"].startswith("https://goatza.test/squad-invite/"))
        self.assertEqual(
            Squad.objects.get(id=data["id"]).created_by_member, self.owner_member
        )

    def test_names_are_unique_ignoring_case(self):
        response = self.org_post(self.owner, "create", {
            "name": "u17 boys", "sport_id": str(self.football.id),
        })
        self.assert_error(response, "name_taken")

        response = self.org_patch(
            self.owner, f"{self.make_squad('Seniors').id}/update", {"name": "U17 BOYS"}
        )
        self.assert_error(response, "name_taken")

    def test_bad_fields(self):
        sport = str(self.football.id)
        for body, code in (
            ({"name": "A", "sport_id": sport}, "invalid_name"),
            ({"name": "Seniors", "sport_id": str(uuid.uuid4())}, "invalid_sport"),
            ({"name": "Seniors"}, "invalid_sport"),
            ({"name": "Seniors", "sport_id": sport, "level": "pro"}, "invalid_field"),
            ({"name": "Seniors", "sport_id": sport, "description": "x" * 601}, "invalid_field"),
        ):
            self.assert_error(self.org_post(self.owner, "create", body), code)

    def test_squad_limit(self):
        for index in range(49):
            self.make_squad(f"Squad {index}")

        response = self.org_post(self.owner, "create", {
            "name": "One too many", "sport_id": str(self.football.id),
        })
        self.assert_error(response, "squad_limit")

    def test_the_sport_locks_once_the_squad_has_players(self):
        path = f"{self.squad.id}/update"

        SquadMember.objects.create(
            squad=self.squad, user=self.p1, status=Status.INVITED,
            source=SquadMember.Source.INVITE,
            expires_at=timezone.now() - timedelta(days=1),
        )
        # An expired invite is nobody: the sport can still change.
        response = self.org_patch(self.owner, path, {"sport_id": str(self.cricket.id)})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(SquadMember.objects.filter(squad=self.squad).exists())

        SquadMember.objects.create(
            squad=self.squad, user=self.p1, status=Status.PENDING,
            source=SquadMember.Source.LINK,
        )
        response = self.org_patch(self.owner, path, {"sport_id": str(self.football.id)})
        self.assert_error(response, "sport_locked")

    def test_partial_update(self):
        response = self.org_patch(
            self.owner, f"{self.squad.id}/update",
            {"is_public": False, "season": "2026–27"},
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.squad.refresh_from_db()
        self.assertFalse(self.squad.is_public)
        self.assertEqual(self.squad.name, "U17 Boys")


class DeleteTests(SquadTestBase):

    def test_soft_delete_closes_career_entries_and_clears_requests_and_invites(self):
        owner = self.owner_actor()
        self.squad.photo_url = f"{MEDIA}/organizations/{self.org.id}/squads/a.webp"
        self.squad.photo_public_id = f"organizations/{self.org.id}/squads/a.webp"
        self.squad.save()

        self.run_after_commit(MembershipService.invite, owner, self.squad.id, self.p1.id)
        row = SquadMember.objects.get(user=self.p1)
        self.run_after_commit(MembershipService.accept_invite, self.p1, row.id)
        self.run_after_commit(MembershipService.request_join, self.p2, self.squad)
        self.run_after_commit(MembershipService.invite, owner, self.squad.id, self.p3.id)
        self.squad.staff.create(member=self.coach_member, role="head_coach")

        response = self.org_post(self.owner, f"{self.squad.id}/delete")
        self.assertEqual(response.status_code, 200, response.data)

        self.squad.refresh_from_db()
        self.assertTrue(self.squad.is_deleted)
        self.assertEqual((self.squad.members_count, self.squad.pending_count), (0, 0))
        self.assertEqual(self.squad.photo_url, "")

        row.refresh_from_db()
        self.assertEqual(row.status, Status.LEFT)
        self.assertEqual(row.ended_by, self.owner_member)

        entry = CareerEntry.objects.get(user=self.p1)
        self.assertFalse(entry.is_current)
        self.assertIsNotNone(entry.end_date)
        self.assertEqual(entry.verification_status, CareerEntry.VerificationStatus.VERIFIED)

        self.assertFalse(SquadMember.objects.filter(user__in=[self.p2, self.p3]).exists())
        self.assertFalse(SquadStaff.objects.filter(squad=self.squad).exists())
        self.storage.assert_called_once()
        self.assertEqual(
            self.storage.call_args.args[1], ([f"organizations/{self.org.id}/squads/a.webp"],)
        )

        # Gone from the club's list, and its name is free again.
        self.assertEqual(self.org_get(self.owner, "list").data["data"]["squads"], [])
        self.assert_error(self.org_get(self.owner, f"{self.squad.id}"), "not_found", 404)


class PhotoTests(SquadTestBase):

    def _key(self, folder="squads"):
        return f"organizations/{self.org.id}/{folder}/{uuid.uuid4()}.webp"

    def test_a_photo_from_the_squads_folder(self):
        first = self._key()
        response = self.org_post(self.owner, f"{self.squad.id}/photo", {
            "photo_url": f"{MEDIA}/{first}", "photo_public_id": first,
        })
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["photo_url"], f"{MEDIA}/{first}")
        self.storage.assert_not_called()

        second = self._key()
        self.org_post(self.owner, f"{self.squad.id}/photo", {
            "photo_url": f"{MEDIA}/{second}", "photo_public_id": second,
        })
        # The replaced object is deleted after commit.
        self.assertEqual(self.storage.call_args.args[1], ([first],))

        self.org_post(self.owner, f"{self.squad.id}/photo", {"remove": True})
        self.assertEqual(self.storage.call_args.args[1], ([second],))
        self.squad.refresh_from_db()
        self.assertEqual(self.squad.photo_public_id, "")

    def test_anything_else_is_refused(self):
        other_org = uuid.uuid4()
        for key, url in (
            (self._key("logo"), None),
            (f"organizations/{other_org}/squads/{uuid.uuid4()}.webp", None),
            (self._key(), f"https://evil.example.com/{self._key()}"),
            (f"organizations/{self.org.id}/squads/{uuid.uuid4()}.src.mov", None),
            (f"organizations/{self.org.id}/squads/{uuid.uuid4()}.mp4", None),
        ):
            response = self.org_post(self.owner, f"{self.squad.id}/photo", {
                "photo_url": url or f"{MEDIA}/{key}", "photo_public_id": key,
            })
            self.assert_error(response, "invalid_photo")


class StaffAndLinkTests(SquadTestBase):

    def test_staff(self):
        path = f"{self.squad.id}/staff"
        response = self.org_post(self.owner, path, {
            "member_id": str(self.coach_member.id), "role": "head_coach",
        })
        self.assertEqual(response.status_code, 201, response.data)
        staff = response.data["data"]["staff"]
        self.assertEqual(staff[0]["role_label"], "Head coach")
        self.assertEqual(staff[0]["user"]["name"], "Cora Coach")

        self.assert_error(self.org_post(self.owner, path, {
            "member_id": str(self.coach_member.id), "role": "physio",
        }), "already_staff")
        self.assert_error(self.org_post(self.owner, path, {
            "member_id": str(self.staff_member.id), "role": "chef",
        }), "invalid_field")

        stranger = self._member(
            make_user("stranger", role="coach"), "owner",
            org=Organization.objects.create(name="X", username="xfc", type="club"),
        )
        self.assert_error(self.org_post(self.owner, path, {
            "member_id": str(stranger.id), "role": "physio",
        }), "invalid_staff")

        staff_id = staff[0]["id"]
        response = self.org_post(self.owner, f"{path}/{staff_id}/remove")
        self.assertEqual(response.data["data"]["staff"], [])

    def test_a_removed_org_member_drops_off_the_staff_list(self):
        self.squad.staff.create(member=self.coach_member, role="head_coach")
        OrganizationMember.objects.filter(id=self.coach_member.id).update(
            status=OrganizationMember.Status.REMOVED
        )

        detail = self.org_get(self.owner, f"{self.squad.id}").data["data"]
        self.assertEqual(detail["staff"], [])
        # The row itself is kept.
        self.assertTrue(SquadStaff.objects.filter(squad=self.squad).exists())

    def test_the_invite_link(self):
        path = f"{self.squad.id}/invite-link"
        response = self.org_patch(self.owner, path, {"enabled": False})
        self.assertEqual(response.data["data"]["enabled"], False)
        self.assert_error(self.org_patch(self.owner, path, {"enabled": "no"}), "invalid_field")

        old = self.squad.invite_token
        response = self.org_post(self.owner, f"{path}/reset")
        self.squad.refresh_from_db()
        self.assertNotEqual(self.squad.invite_token, old)
        self.assertTrue(response.data["data"]["url"].endswith(self.squad.invite_token))

    def test_the_list_filters_by_sport_and_counts_open_invites(self):
        self.make_squad("Cricket XI", sport=self.cricket)
        self.run_after_commit(
            MembershipService.invite, self.owner_actor(), self.squad.id, self.p1.id
        )
        SquadMember.objects.create(
            squad=self.squad, user=self.p2, status=Status.INVITED,
            source=SquadMember.Source.INVITE,
            expires_at=timezone.now() - timedelta(minutes=1),
        )

        squads = self.org_get(self.owner, "list", sport_id=str(self.football.id)).data["data"]["squads"]
        self.assertEqual([squad["name"] for squad in squads], ["U17 Boys"])
        self.assertEqual(squads[0]["open_invites_count"], 1)
        self.assertEqual(len(self.org_get(self.owner, "list").data["data"]["squads"]), 2)

    def test_candidates(self):
        self.run_after_commit(
            MembershipService.invite, self.owner_actor(), self.squad.id, self.p1.id
        )
        self.primary_position(self.p2, self.striker)
        make_user("bilalcoach", "Bilal Coach", role="coach")

        response = self.org_get(self.coach, f"{self.squad.id}/candidates", q="bil")
        results = response.data["data"]["results"]
        self.assertEqual([r["user"]["username"] for r in results], ["bilal"])
        self.assertEqual(results[0]["primary_position"]["name"], "Striker")
        self.assertEqual(results[0]["state"], "none")

        results = self.org_get(self.coach, f"{self.squad.id}/candidates", q="asha").data["data"]["results"]
        self.assertEqual(results[0]["state"], "invited")

        self.assertEqual(
            self.org_get(self.coach, f"{self.squad.id}/candidates", q="a").data["data"]["results"], []
        )

        self.block(self.org, target_user=self.p2)
        results = self.org_get(self.coach, f"{self.squad.id}/candidates", q="bil").data["data"]["results"]
        self.assertEqual(results, [])
