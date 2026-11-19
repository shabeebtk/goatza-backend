"""Coaches: a member, or somebody not on Goatza."""

from django.db import IntegrityError, transaction

from apps.organization.models import OrganizationMember
from apps.orgpages.models import Coach
from apps.orgpages.tests.base import MEDIA, PageTestBase


class CoachModelTests(PageTestBase):

    def test_member_or_name_exactly_one(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Coach.objects.create(
                organization=self.org, member=self.staff_member, name="Sam", role="head_coach"
            )
        with self.assertRaises(IntegrityError), transaction.atomic():
            Coach.objects.create(organization=self.org, role="head_coach")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Coach.objects.create(
                organization=self.org, member=self.staff_member, role="head_coach",
                photo_url=f"{MEDIA}/x.webp",
            )
        Coach.objects.create(organization=self.org, member=self.staff_member, role="head_coach")
        Coach.objects.create(organization=self.org, name="Ravi Kumar", role="fitness_coach")


class CoachApiTests(PageTestBase):

    def test_add_a_member_coach_and_a_named_one(self):
        response = self.org_post(self.owner, "coaches", {
            "member_id": str(self.staff_member.id), "role": "head_coach",
            "licence": "AFC B", "years_coaching": 12,
        })
        self.assertEqual(response.status_code, 201, response.data)
        self.assertTrue(response.data["data"]["is_member"])
        self.assertEqual(response.data["data"]["name"], "Sam Staff")

        key = self.page_key()
        response = self.org_post(self.owner, "coaches", {
            "name": "Ravi Kumar", "role": "other", "title": "Girls coach",
            "photo_url": f"{MEDIA}/{key}", "photo_public_id": key,
        })
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["data"]["role_label"], "Girls coach")
        self.assertEqual(response.data["data"]["photo"], f"{MEDIA}/{key}")

        data = self.org_get(self.owner, "coaches").data["data"]
        self.assertEqual(len(data["coaches"]), 2)
        candidate_ids = {c["member_id"] for c in data["candidates"]}
        self.assertNotIn(str(self.staff_member.id), candidate_ids)
        self.assertIn(str(self.coach_member.id), candidate_ids)

    def test_the_rules(self):
        self.assert_error(self.org_post(self.owner, "coaches", {"name": "Ravi"}), "invalid", field="role")
        self.assert_error(
            self.org_post(self.owner, "coaches", {"role": "head_coach"}), "invalid", field="name"
        )
        self.assert_error(
            self.org_post(self.owner, "coaches", {
                "role": "head_coach", "name": "Ravi", "member_id": str(self.staff_member.id),
            }),
            "invalid", field="name",
        )
        self.assert_error(
            self.org_post(self.owner, "coaches", {"role": "other", "name": "Ravi"}),
            "invalid", field="title",
        )
        self.assert_error(
            self.org_post(self.owner, "coaches", {
                "role": "head_coach", "member_id": str(self.staff_member.id),
                **self.photo_body(),
            }),
            "invalid", field="photo_url",
        )
        self.assert_error(
            self.org_post(self.owner, "coaches", {
                "role": "head_coach", "name": "Ravi", "years_coaching": 61,
            }),
            "invalid", field="years_coaching",
        )

    def test_a_duplicate_member_is_refused(self):
        body = {"member_id": str(self.staff_member.id), "role": "head_coach"}
        self.assertEqual(self.org_post(self.owner, "coaches", body).status_code, 201)
        self.assert_error(self.org_post(self.owner, "coaches", body), "already_added")

    def test_a_member_of_another_org_is_refused(self):
        other = self.make_org("Other", "otheracademy")
        stranger = self._member(self.p1, OrganizationMember.Role.STAFF, org=other)
        self.assert_error(
            self.org_post(self.owner, "coaches", {"member_id": str(stranger.id), "role": "head_coach"}),
            "invalid", field="member_id",
        )

    def test_hide_edit_and_replace_the_photo(self):
        first = self.page_key()
        coach_id = self.org_post(self.owner, "coaches", {
            "name": "Ravi", "role": "fitness_coach", **self.photo_body(first),
        }).data["data"]["id"]

        response = self.org_patch(self.owner, f"coaches/{coach_id}", {"is_visible": False})
        self.assertTrue(response.data["data"]["is_hidden"])

        second = self.page_key()
        self.org_patch(self.owner, f"coaches/{coach_id}", self.photo_body(second))
        self.assertEqual(self.storage.call_args.args[1], ([first],))

        self.org_post(self.owner, f"coaches/{coach_id}/delete")
        self.assertEqual(self.storage.call_args.args[1], ([second],))
        self.assertFalse(Coach.objects.filter(id=coach_id).exists())

    def test_a_member_coachs_name_is_their_profiles(self):
        coach_id = self.org_post(self.owner, "coaches", {
            "member_id": str(self.staff_member.id), "role": "head_coach",
        }).data["data"]["id"]
        self.assert_error(
            self.org_patch(self.owner, f"coaches/{coach_id}", {"name": "Someone else"}),
            "invalid", field="name",
        )

    def test_a_removed_members_coach_disappears(self):
        self.org_post(self.owner, "coaches", {
            "member_id": str(self.staff_member.id), "role": "head_coach",
        })
        self.org_post(self.owner, "coaches", {"name": "Ravi", "role": "fitness_coach"})
        names = [c["name"] for c in self.public_page().data["data"]["coaches"]]
        self.assertEqual(names, ["Sam Staff", "Ravi"])

        OrganizationMember.all_objects.filter(id=self.staff_member.id).update(
            status=OrganizationMember.Status.REMOVED
        )
        from django.core.cache import cache
        cache.clear()

        names = [c["name"] for c in self.public_page().data["data"]["coaches"]]
        self.assertEqual(names, ["Ravi"])
        editor = self.org_get(self.owner, "coaches").data["data"]["coaches"]
        self.assertEqual([c["name"] for c in editor], ["Ravi"])

    def test_reorder(self):
        a = self.org_post(self.owner, "coaches", {"name": "Anu", "role": "head_coach"}).data["data"]["id"]
        b = self.org_post(self.owner, "coaches", {"name": "Bala", "role": "head_coach"}).data["data"]["id"]
        self.assert_error(self.org_post(self.owner, "coaches/reorder", {"ids": [a]}), "invalid", field="ids")
        response = self.org_post(self.owner, "coaches/reorder", {"ids": [b, a]})
        self.assertEqual([c["name"] for c in response.data["data"]["coaches"]], ["Bala", "Anu"])
