"""The org's inbox."""

import uuid
from datetime import timedelta

from django.utils import timezone

from apps.enquiries.models import Enquiry
from apps.enquiries.tests.base import EnquiryTestBase
from apps.organization.permissions import DENIED_MESSAGES


class InboxTests(EnquiryTestBase):

    def test_counts_follow_the_filters_but_not_the_status(self):
        other_program = self.make_program("U16")
        self.make_enquiry(program=self.program)
        self.make_enquiry(program=self.program, status=Enquiry.Status.CONTACTED)
        self.make_enquiry(program=other_program, status=Enquiry.Status.JOINED)

        data = self.inbox_get(self.staff, status="new").data["data"]
        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(
            data["counts"], {"new": 1, "contacted": 1, "joined": 1, "not_interested": 0}
        )

        data = self.inbox_get(self.owner, program_id=str(self.program.id)).data["data"]
        self.assertEqual(len(data["items"]), 2)
        self.assertEqual(data["counts"]["joined"], 0)

        self.assert_error(self.inbox_get(self.owner, status="spam"), "invalid", field="status")

    def test_an_item(self):
        self.make_enquiry(message="m" * 300, program=self.program, program_name="U12 Football")
        item = self.inbox_get(self.owner).data["data"]["items"][0]
        self.assertEqual(len(item["message"]), 120)
        self.assertEqual(item["program_name"], "U12 Football")
        self.assertEqual(item["phone"], "+919000000001")
        self.assertNotIn("notes", item)

    def test_cursor_pages(self):
        base = timezone.now()
        for i in range(25):
            enquiry = self.make_enquiry(player_name=f"Kid {i:02d}")
            Enquiry.objects.filter(id=enquiry.id).update(created_at=base - timedelta(minutes=i))

        first = self.inbox_get(self.owner).data["data"]
        self.assertEqual(len(first["items"]), 20)
        self.assertEqual(first["items"][0]["player_name"], "Kid 00")
        self.assertIsNotNone(first["next_cursor"])

        second = self.inbox_get(self.owner, cursor=first["next_cursor"]).data["data"]
        self.assertEqual(len(second["items"]), 5)
        self.assertIsNone(second["next_cursor"])
        names = [i["player_name"] for i in first["items"] + second["items"]]
        self.assertEqual(len(set(names)), 25)

        self.assert_error(self.inbox_get(self.owner, cursor="garbage!"), "invalid", field="cursor")

    def test_another_orgs_enquiry_or_an_unknown_id_is_a_404(self):
        other = self.make_org("Other", "otheracademy")
        theirs = self.make_enquiry(org=other)
        self.assert_error(self.inbox_get(self.owner, f"{theirs.id}"), "not_found", 404)
        self.assert_error(self.inbox_get(self.owner, f"{uuid.uuid4()}"), "not_found", 404)
        self.assert_error(
            self.inbox_patch(self.owner, theirs.id, {"status": "contacted"}), "not_found", 404
        )

    def test_a_coach_is_refused(self):
        enquiry = self.make_enquiry()
        for response in (
            self.inbox_get(self.coach),
            self.inbox_get(self.coach, "inbox/unread-count"),
            self.inbox_get(self.coach, f"{enquiry.id}"),
            self.inbox_patch(self.coach, enquiry.id, {"status": "contacted"}),
        ):
            self.assert_error(response, "forbidden", 403)
            self.assertEqual(response.data["message"], DENIED_MESSAGES["enquiries.manage"])

    def test_patch_records_who_and_when_and_closed_at(self):
        enquiry = self.make_enquiry()

        data = self.inbox_patch(self.staff, enquiry.id, {"status": "contacted"}).data["data"]
        self.assertEqual(data["status"], "contacted")
        self.assertEqual(data["status_changed_by"], "Sam Staff")
        self.assertIsNotNone(data["status_changed_at"])
        self.assertIsNone(data["closed_at"])

        data = self.inbox_patch(self.owner, enquiry.id, {"status": "joined"}).data["data"]
        self.assertEqual(data["status_changed_by"], "Olive Owner")
        self.assertIsNotNone(data["closed_at"])

        data = self.inbox_patch(self.owner, enquiry.id, {"status": "new"}).data["data"]
        self.assertIsNone(data["closed_at"])

        self.assert_error(
            self.inbox_patch(self.owner, enquiry.id, {"status": "archived"}), "invalid", field="status"
        )

    def test_notes(self):
        enquiry = self.make_enquiry()
        data = self.inbox_patch(self.staff, enquiry.id, {"notes": "  Called, coming Saturday  "}).data["data"]
        self.assertEqual(data["notes"], "Called, coming Saturday")
        self.assertEqual(data["notes_updated_by"], "Sam Staff")
        self.assertIsNotNone(data["notes_updated_at"])
        self.assertEqual(data["status"], "new")

        detail = self.inbox_get(self.owner, f"{enquiry.id}").data["data"]
        self.assertEqual(detail["notes"], "Called, coming Saturday")
        self.assert_error(
            self.inbox_patch(self.owner, enquiry.id, {"notes": "x" * 2001}), "invalid", field="notes"
        )

    def test_unread_count(self):
        self.make_enquiry()
        self.make_enquiry(status=Enquiry.Status.CONTACTED)
        self.assertEqual(self.inbox_get(self.owner, "inbox/unread-count").data["data"], {"new": 1})
