"""page.manage and enquiries.manage: owners and staff, never a coach by default."""

import uuid

from apps.organization.permissions import (
    DENIED_MESSAGES,
    OrgPermission,
    has_org_permission,
)
from apps.orgpages.tests.base import PageTestBase


class PagePermissionTests(PageTestBase):

    def test_owner_and_staff_hold_both_and_a_coach_neither(self):
        for perm in (OrgPermission.PAGE_MANAGE, OrgPermission.ENQUIRIES_MANAGE):
            self.assertTrue(has_org_permission(self.owner_member, perm))
            self.assertTrue(has_org_permission(self.staff_member, perm))
            self.assertFalse(has_org_permission(self.coach_member, perm))

    def test_they_reach_my_permissions(self):
        self.client.force_authenticate(user=self.staff)
        row = self.client.get("/organizations/list").data["data"][0]
        self.assertIn("page.manage", row["my_permissions"])
        self.assertIn("enquiries.manage", row["my_permissions"])

        self.client.force_authenticate(user=self.coach)
        row = self.client.get("/organizations/list").data["data"][0]
        self.assertNotIn("page.manage", row["my_permissions"])
        self.assertNotIn("enquiries.manage", row["my_permissions"])

    def test_every_editor_endpoint_answers_a_coach_403(self):
        some_id = uuid.uuid4()
        branch = self.branch.id
        endpoints = [
            ("get", "programs"), ("post", "programs"),
            ("patch", f"programs/{some_id}"), ("post", f"programs/{some_id}/delete"),
            ("post", "programs/reorder"),
            ("patch", "contact"), ("patch", "facts"), ("patch", "sections"),
            ("get", "facilities"), ("put", f"facilities/{branch}"),
            ("post", f"facilities/{branch}/photos"),
            ("post", f"facilities/photos/{some_id}/delete"),
            ("post", f"facilities/{branch}/photos/reorder"),
            ("get", "coaches"), ("post", "coaches"),
            ("patch", f"coaches/{some_id}"), ("post", f"coaches/{some_id}/delete"),
            ("post", "coaches/reorder"),
            ("get", "players"), ("post", "players"),
            ("patch", f"players/{some_id}"), ("post", f"players/{some_id}/remove"),
            ("post", "players/reorder"),
        ]
        for method, path in endpoints:
            if method == "get":
                response = self.org_get(self.coach, path)
            else:
                response = getattr(self, f"org_{method}")(self.coach, path, {"x": 1})
            self.assert_error(response, "forbidden", 403)
            self.assertEqual(
                response.data["message"], DENIED_MESSAGES["page.manage"], path
            )

    def test_the_hub_is_readable_by_any_member_but_only_editable_by_page_managers(self):
        response = self.org_get(self.coach, "admin")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(response.data["data"]["can_edit"])

        response = self.org_get(self.staff, "admin")
        self.assertTrue(response.data["data"]["can_edit"])

    def test_a_person_acting_as_themselves_is_told_to_switch(self):
        self.client.force_authenticate(user=self.owner)
        self.assert_error(self.client.get("/page/programs"), "switch_to_org", 403)
