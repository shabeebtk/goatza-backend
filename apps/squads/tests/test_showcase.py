"""The showcase: what everyone else sees, signed in and logged out."""

from django.core.cache import cache
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.accounts.models import UserProfile
from apps.organization.models import OrganizationProfile
from apps.squads.models import Squad
from apps.squads.services.membership_service import MembershipService
from apps.squads.tests.base import SquadTestBase, child_birthdate, find_keys, make_user

# Nothing a stranger should learn about a player or how they got in.
PRIVATE_KEYS = {
    "age", "birthdate", "location", "location_name", "notes", "source",
    "requested_at", "expires_at", "invited_by_name", "pending_count",
    "open_invites_count", "invite", "invite_token", "career_entry_id",
}


class ShowcaseTestBase(SquadTestBase):

    def setUp(self):
        super().setUp()
        self.viewer = make_user("viewer", "Vera Viewer")
        self.captain = self.activate(
            self.squad, self.p1, position=self.gk, jersey_number=1, is_captain=True
        )
        self.activate(self.squad, self.p2, position=self.striker, jersey_number=9)
        self.activate(self.squad, self.p3)
        self.squad.staff.create(member=self.coach_member, role="head_coach")

    def showcase(self, viewer=None, squad=None):
        path = f"showcase/dreamfc/{squad.id}" if squad else "showcase/dreamfc"
        return self.user_get(viewer or self.viewer, path)

    def public(self, squad=None):
        path = "/public/organization/dreamfc/squads"
        return self.anon_get(f"{path}/{squad.id}" if squad else path)

    def hide_profile(self, user):
        UserProfile.objects.filter(user=user).update(is_public_profile=False)


class SignedInShowcaseTests(ShowcaseTestBase):

    def test_list(self):
        response = self.showcase()
        self.assertEqual(response.status_code, 200, response.data)
        data = response.data["data"]

        self.assertEqual(data["organization"]["username"], "dreamfc")
        squad = data["squads"][0]
        self.assertEqual(squad["members_count"], 3)
        self.assertEqual(squad["head_coach_name"], "Cora Coach")
        # The captain leads the preview.
        self.assertEqual(squad["preview_members"][0]["name"], "Asha Player")

    def test_squad_page(self):
        data = self.showcase(squad=self.squad).data["data"]

        self.assertEqual(
            [(group["name"], group["count"]) for group in data["groups"]],
            [("Goalkeepers", 1), ("Forwards", 1), ("Other", 1)],
        )
        self.assertEqual(data["captain_member_id"], str(self.captain.id))
        self.assertEqual(data["counts"], {"players": 3, "staff": 1})
        self.assertEqual(data["staff"][0]["role_label"], "Head coach")
        self.assertEqual(data["config"]["member_noun_plural"], "players")
        member = data["groups"][0]["members"][0]
        self.assertEqual(
            set(member),
            {"id", "name", "username", "photo", "position", "jersey_number", "is_captain"},
        )
        self.assertTrue(member["photo"])

    def test_hidden_and_empty_squads_are_404_even_for_the_club(self):
        hidden = self.make_squad("Hidden", is_public=False)
        self.activate(hidden, self.p1)
        empty = self.make_squad("Empty")

        for viewer in (self.viewer, self.owner):
            names = [s["name"] for s in self.showcase(viewer).data["data"]["squads"]]
            self.assertEqual(names, ["U17 Boys"])
            for squad in (hidden, empty):
                self.assert_error(self.showcase(viewer, squad), "not_found", 404)

        # Acting as the org changes nothing either.
        response = self.client.get(
            f"/squads/showcase/dreamfc/{hidden.id}", **self._org_headers(self.owner)
        )
        self.assert_error(response, "not_found", 404)

    def test_blocks(self):
        self.block(self.viewer, target_user=self.p2)
        data = self.showcase(squad=self.squad).data["data"]
        names = [m["name"] for g in data["groups"] for m in g["members"]]
        self.assertNotIn("Bilal Player", names)
        # Dropped from the counts too: a count that disagreed would tell them.
        self.assertEqual(data["counts"]["players"], 2)
        self.assertTrue(all(g["hidden_count"] == 0 for g in data["groups"]))

        self.block(self.org, target_user=self.viewer)
        self.assert_error(self.showcase(), "not_found", 404)
        self.assert_error(self.showcase(squad=self.squad), "not_found", 404)

    def test_a_removed_org_member_leaves_the_staff_list(self):
        from apps.organization.models import OrganizationMember

        OrganizationMember.objects.filter(id=self.coach_member.id).update(status="removed")
        data = self.showcase(squad=self.squad).data["data"]
        self.assertEqual(data["staff"], [])
        self.assertIsNone(self.showcase().data["data"]["squads"][0]["head_coach_name"])

    def test_other_squads(self):
        seniors = self.make_squad("Seniors")
        self.activate(seniors, self.viewer)
        hidden = self.make_squad("Hidden", is_public=False)
        self.activate(hidden, self.viewer)

        data = self.showcase(squad=self.squad).data["data"]
        self.assertEqual(
            [(s["name"], s["members_count"]) for s in data["other_squads"]],
            [("Seniors", 1)],
        )


class AnonymousShowcaseTests(ShowcaseTestBase):

    def test_private_profiles_are_counted_not_listed(self):
        self.hide_profile(self.p3)

        squad = self.public().data["data"]["squads"][0]
        self.assertEqual(squad["members_count"], 3)
        self.assertNotIn("Chen Player", [p["name"] for p in squad["preview_members"]])

        data = self.public(self.squad).data["data"]
        other = next(g for g in data["groups"] if g["name"] == "Other")
        self.assertEqual((other["count"], other["hidden_count"], other["members"]), (1, 1, []))
        self.assertEqual(data["counts"]["players"], 3)

    def test_a_private_captain_is_not_named(self):
        self.hide_profile(self.p1)
        self.assertIsNone(self.public(self.squad).data["data"]["captain_member_id"])

    def test_no_photo_of_a_minor_players_and_staff_alike(self):
        UserProfile.objects.filter(user__in=[self.p2, self.coach]).update(
            birthdate=child_birthdate()
        )

        data = self.public(self.squad).data["data"]
        photos = {m["name"]: m["photo"] for g in data["groups"] for m in g["members"]}
        self.assertEqual(photos["Bilal Player"], "")
        self.assertTrue(photos["Asha Player"])
        self.assertEqual(data["staff"][0]["photo"], "")

        preview = self.public().data["data"]["squads"][0]["preview_members"]
        self.assertEqual({p["name"]: p["photo"] for p in preview}["Bilal Player"], "")

        # Signed in, photos as everywhere in the app.
        data = self.showcase(squad=self.squad).data["data"]
        photos = {m["name"]: m["photo"] for g in data["groups"] for m in g["members"]}
        self.assertTrue(photos["Bilal Player"])

    def test_the_orgs_own_public_setting(self):
        OrganizationProfile.objects.filter(organization=self.org).update(is_public_profile=False)
        self.assert_error(self.public(), "not_found", 404)
        self.assert_error(self.public(self.squad), "not_found", 404)
        self.assertEqual(self.showcase().status_code, 200)

    def test_a_squad_whose_players_are_all_private_is_a_miss(self):
        for user in (self.p1, self.p2, self.p3):
            self.hide_profile(user)
        self.assertEqual(self.public().data["data"]["squads"], [])
        self.assert_error(self.public(self.squad), "not_found", 404)

    def test_cached_and_cleared_by_a_roster_change(self):
        self.assertEqual(self.public(self.squad).data["data"]["counts"]["players"], 3)

        # Served from the cache. (Logged out already, so the test client's own
        # session queries are not in the count.)
        with self.assertNumQueries(0):
            self.client.get(f"/public/organization/dreamfc/squads/{self.squad.id}")

        row, _ = MembershipService.request_join(self.viewer, self.squad)
        self.run_after_commit(
            MembershipService.approve, self.owner_actor(), self.squad.id,
            [{"member_id": str(row.id)}],
        )
        self.assertEqual(self.public(self.squad).data["data"]["counts"]["players"], 4)

    def test_cleared_when_the_squad_is_hidden(self):
        self.assertEqual(len(self.public().data["data"]["squads"]), 1)
        self.org_patch(self.owner, f"{self.squad.id}/update", {"is_public": False})
        self.assertEqual(self.public().data["data"]["squads"], [])


class StaffPrivacyTests(ShowcaseTestBase):
    """Staff follow the same rules as players, on every surface."""

    def staff_names(self, data):
        return [staff["name"] for staff in data["staff"]]

    def test_anonymous_leaves_out_a_private_profile_staff_member(self):
        self.hide_profile(self.coach)

        data = self.public(self.squad).data["data"]
        self.assertEqual(data["staff"], [])
        self.assertEqual(data["counts"]["staff"], 0)
        self.assertIsNone(self.public().data["data"]["squads"][0]["head_coach_name"])

        # Signed in, a private profile is an ordinary one.
        data = self.showcase(squad=self.squad).data["data"]
        self.assertEqual(self.staff_names(data), ["Cora Coach"])
        self.assertEqual(self.showcase().data["data"]["squads"][0]["head_coach_name"], "Cora Coach")

    def test_a_listed_head_coach_is_still_named(self):
        self.squad.staff.create(member=self.staff_member, role="head_coach", order=1)
        self.hide_profile(self.coach)

        squad = self.public().data["data"]["squads"][0]
        self.assertEqual(squad["head_coach_name"], "Sam Staff")

    def test_a_deactivated_or_deleting_staff_member_is_hidden_everywhere(self):
        from apps.accounts.models import User
        from django.utils import timezone

        for change in ({"is_active": False}, {"deletion_requested_at": timezone.now()}):
            User.objects.filter(id=self.coach.id).update(**change)
            cache.clear()

            for data in (
                self.public(self.squad).data["data"],
                self.showcase(squad=self.squad).data["data"],
            ):
                self.assertEqual(data["staff"], [])
                self.assertEqual(data["counts"]["staff"], 0)
            self.assertIsNone(self.public().data["data"]["squads"][0]["head_coach_name"])
            self.assertIsNone(self.showcase().data["data"]["squads"][0]["head_coach_name"])

            User.objects.filter(id=self.coach.id).update(
                is_active=True, deletion_requested_at=None
            )

    def test_staff_in_a_block_with_the_viewer_are_hidden_for_that_viewer(self):
        self.block(self.viewer, target_user=self.coach)

        data = self.showcase(squad=self.squad).data["data"]
        self.assertEqual(data["staff"], [])
        self.assertEqual(data["counts"]["staff"], 0)
        self.assertIsNone(self.showcase().data["data"]["squads"][0]["head_coach_name"])

        # Anybody else still sees them.
        data = self.showcase(self.p2, squad=self.squad).data["data"]
        self.assertEqual(self.staff_names(data), ["Cora Coach"])

    def test_the_list_and_the_page_agree_on_the_player_count(self):
        self.hide_profile(self.p3)
        self.block(self.viewer, target_user=self.p2)

        anonymous_card = self.public().data["data"]["squads"][0]
        anonymous_page = self.public(self.squad).data["data"]
        self.assertEqual(anonymous_card["members_count"], anonymous_page["counts"]["players"])
        self.assertEqual(anonymous_card["members_count"], 3)

        signed_in_card = self.showcase().data["data"]["squads"][0]
        signed_in_page = self.showcase(squad=self.squad).data["data"]
        self.assertEqual(signed_in_card["members_count"], signed_in_page["counts"]["players"])
        self.assertEqual(signed_in_card["members_count"], 2)

        # The squad page's "other squads" card says the same as the list.
        seniors = self.make_squad("Seniors")
        self.activate(seniors, self.p1)
        self.activate(seniors, self.p3)
        cache.clear()
        others = self.public(self.squad).data["data"]["other_squads"]
        card = next(s for s in self.public().data["data"]["squads"] if s["name"] == "Seniors")
        self.assertEqual(others[0]["members_count"], card["members_count"])
        self.assertEqual(card["members_count"], 2)


class ShowcasePayloadTests(ShowcaseTestBase):

    def test_nothing_private_on_any_payload(self):
        self.captain.notes = "private note"
        self.captain.save()

        payloads = [
            self.showcase().data["data"],
            self.showcase(squad=self.squad).data["data"],
            self.public().data["data"],
            self.public(self.squad).data["data"],
        ]
        for payload in payloads:
            self.assertEqual(find_keys(payload, PRIVATE_KEYS), set(), payload)
            self.assertNotIn("private note", str(payload))

    def _count_queries(self, call):
        cache.clear()
        with CaptureQueriesContext(connection) as queries:
            response = call()
        self.assertEqual(response.status_code, 200, response.data)
        return len(queries)

    def test_no_query_per_player(self):
        anonymous_before = self._count_queries(lambda: self.public(self.squad))
        signed_in_before = self._count_queries(lambda: self.showcase(squad=self.squad))

        for index in range(6):
            self.activate(self.squad, make_user(f"extra{index}"), position=self.defender)
        self.squad.staff.create(member=self.staff_member, role="physio")

        self.assertEqual(self._count_queries(lambda: self.public(self.squad)), anonymous_before)
        self.assertEqual(
            self._count_queries(lambda: self.showcase(squad=self.squad)), signed_in_before
        )

    def test_the_anonymous_squad_page_is_four_queries(self):
        # The org, every squad, every player, every staff row.
        self.client.force_authenticate(user=None)
        cache.clear()
        with self.assertNumQueries(4):
            response = self.client.get(
                f"/public/organization/dreamfc/squads/{self.squad.id}"
            )
        self.assertEqual(response.status_code, 200)


class SquadSitemapTests(ShowcaseTestBase):
    """
    GET /public/sitemap/urls "squads": a squad is listed exactly when the
    ANONYMOUS showcase would show it.
    """

    def feed_squads(self):
        cache.clear()
        response = self.anon_get("/public/sitemap/urls")
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]["squads"]

    def listed_ids(self):
        return [row["squad_id"] for row in self.feed_squads()]

    def test_a_public_squad_is_listed_with_its_org_and_a_timestamp(self):
        rows = self.feed_squads()

        self.assertEqual(
            [(row["org_username"], row["squad_id"]) for row in rows],
            [("dreamfc", str(self.squad.id))],
        )
        self.assertEqual(sorted(rows[0]), ["org_username", "squad_id", "updated_at"])
        # And the page it points at really opens for a logged-out visitor.
        self.assertEqual(self.public(self.squad).status_code, 200)

    def test_a_hidden_squad_is_left_out(self):
        Squad.objects.filter(id=self.squad.id).update(is_public=False)
        self.assertEqual(self.listed_ids(), [])
        self.assertEqual(self.public(self.squad).status_code, 404)

    def test_an_empty_squad_is_left_out(self):
        empty = self.make_squad("Seniors")
        self.assertEqual(self.listed_ids(), [str(self.squad.id)])
        self.assertNotIn(str(empty.id), self.listed_ids())

    def test_a_squad_whose_players_are_all_private_is_left_out(self):
        # Counted on the page, never listed by name: nothing for a crawler.
        for player in (self.p1, self.p2, self.p3):
            self.hide_profile(player)
        self.assertEqual(self.listed_ids(), [])

    def test_a_squad_of_a_private_org_is_left_out(self):
        OrganizationProfile.objects.filter(organization=self.org).update(
            is_public_profile=False
        )
        self.assertEqual(self.listed_ids(), [])
        self.assertEqual(self.public(self.squad).status_code, 404)

    def test_a_deleted_squad_is_left_out(self):
        Squad.objects.filter(id=self.squad.id).update(is_deleted=True)
        self.assertEqual(self.listed_ids(), [])

    def test_one_query_for_every_club(self):
        from apps.organization.models import Organization
        from apps.squads.selectors.showcase_selectors import public_squad_sitemap_rows

        for index in range(3):
            club = Organization.objects.create(
                name=f"Club {index}", username=f"club{index}", type=Organization.Type.CLUB
            )
            OrganizationProfile.objects.create(organization=club)
            for name in ("A", "B"):
                squad = self.make_squad(f"{name} squad", org=club)
                self.activate(squad, self.p1)

        with self.assertNumQueries(1):
            rows = public_squad_sitemap_rows()
        self.assertEqual(len(rows), 7)
