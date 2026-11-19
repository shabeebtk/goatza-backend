"""One journey: the owner fills the page, a parent enquires, staff work it."""

from apps.enquiries.tests.base import EnquiryTestBase
from apps.notifications.models import Notification
from apps.orgpages.models import Program
from apps.orgpages.tests.base import program_body


class EndToEndTests(EnquiryTestBase):

    def test_page_to_enquiry_to_inbox(self):
        Program.objects.all().delete()

        # The owner adds a program and a WhatsApp number.
        response = self.org_post(
            self.owner, "programs",
            program_body(self.football, name="U10 Weekend", location_id=str(self.branch.id)),
        )
        self.assertEqual(response.status_code, 201, response.data)
        program_id = response.data["data"]["id"]
        response = self.org_patch(self.owner, "contact", {
            "whatsapp_enabled": True, "whatsapp_number": "98470 12345",
        })
        self.assertEqual(response.status_code, 200, response.data)

        # The anonymous public page shows both.
        page = self.public_page().data["data"]
        self.assertEqual([p["name"] for p in page["programs"]], ["U10 Weekend"])
        self.assertEqual(page["contact"]["whatsapp"], "+919847012345")
        self.assertTrue(page["contact"]["enquiry_form"])

        # An anonymous parent enquires about that program.
        response = self.submit(body=self.body(program_id=program_id))
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["data"]["enquiry"]["program_name"], "U10 Weekend")

        # Staff see it in the inbox and got one notification.
        inbox = self.inbox_get(self.staff).data["data"]
        self.assertEqual(inbox["counts"]["new"], 1)
        enquiry_id = inbox["items"][0]["id"]
        self.assertEqual(
            Notification.objects.filter(
                recipient_user=self.staff, type=Notification.Type.ENQUIRY_RECEIVED
            ).count(),
            1,
        )

        # Contacted: the counts move.
        response = self.inbox_patch(self.staff, enquiry_id, {"status": "contacted"})
        self.assertEqual(response.status_code, 200, response.data)
        counts = self.inbox_get(self.staff).data["data"]["counts"]
        self.assertEqual(counts["new"], 0)
        self.assertEqual(counts["contacted"], 1)
