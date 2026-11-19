"""The public enquiry form."""

from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from apps.enquiries.models import Enquiry
from apps.enquiries.tests.base import EnquiryTestBase
from apps.enquiries.throttles import EnquirySubmitThrottle
from apps.enquiries.views.enquiry_views import EnquirySubmitAPIView
from apps.notifications.models import Notification
from apps.organization.models import Organization
from apps.orgpages.models import PageSettings


class SubmitTests(EnquiryTestBase):

    def test_the_happy_path_anonymous(self):
        response = self.submit()
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(
            response.data["data"],
            {"ok": True, "enquiry": {
                "program_name": "U12 Football",
                "location_name": "Main Ground",
                "phone_masked": "+91 98•••• 3210",
            }},
        )
        self.assertNotIn("9876543210", str(response.data))

        enquiry = Enquiry.objects.get()
        self.assertEqual(enquiry.phone, "+919876543210")
        self.assertEqual(enquiry.status, Enquiry.Status.NEW)
        self.assertEqual(enquiry.program_id, self.program.id)
        # The program's own branch fills the location.
        self.assertEqual(enquiry.location_id, self.branch.id)
        self.assertIsNone(enquiry.submitted_by)

    def test_the_happy_path_signed_in(self):
        response = self.submit(user=self.p1, for_whom="self", player_age=24, guardian_name="ignored")
        self.assertEqual(response.status_code, 201, response.data)
        enquiry = Enquiry.objects.get()
        self.assertEqual(enquiry.submitted_by, self.p1)
        self.assertEqual(enquiry.guardian_name, "")

    def test_a_general_enquiry(self):
        response = self.submit(program_id=None)
        self.assertEqual(response.data["data"]["enquiry"]["program_name"], "")
        self.assertEqual(Enquiry.objects.get().location_name, "")

    def test_form_off_is_a_404(self):
        PageSettings.objects.update_or_create(
            organization=self.org, defaults={"enquiry_form_enabled": False}
        )
        self.assert_error(self.submit(), "not_found", 404)
        club = self.make_org("Dream FC", "dreamfc", Organization.Type.CLUB)
        self.assert_error(self.submit(username=club.username), "not_found", 404)
        self.assertFalse(Enquiry.objects.exists())

    def test_a_suspended_or_unknown_org_or_a_block_is_the_same_404(self):
        self.assert_error(self.submit(username="nobodyhere"), "not_found", 404)

        self.block(self.p1, target_org=self.org)
        self.assert_error(self.submit(user=self.p1), "not_found", 404)

        Organization.objects.filter(id=self.org.id).update(is_suspended=True)
        response = self.submit()
        self.assert_error(response, "not_found", 404)
        self.assertEqual(response.data["message"], "This page wasn't found.")
        self.assertFalse(Enquiry.objects.exists())

    def test_the_honeypot_answers_like_a_success_and_stores_nothing(self):
        response = self.submit(website="http://spam.example.com")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["data"]["enquiry"]["phone_masked"], "+91 98•••• 3210")
        self.assertFalse(Enquiry.objects.exists())
        self.assertFalse(Notification.objects.exists())
        self.assertEqual(self.sent_emails(), [])
        self.assertEqual(self.pushes(), 0)

    def test_an_under_18_asking_for_themselves(self):
        response = self.submit(for_whom="self", player_age=16)
        self.assert_error(response, "guardian_needed")
        self.assertEqual(
            response.data["message"],
            "If you're under 18, ask a parent or guardian to send this for you.",
        )
        self.assertFalse(Enquiry.objects.exists())

    def test_field_rules(self):
        other = self.make_org("Other", "otheracademy")
        theirs = self.make_program("Theirs", org=other)
        for overrides, field in (
            ({"program_id": str(theirs.id)}, "program_id"),
            ({"phone": "12345"}, "phone"),
            ({"phone": ""}, "phone"),
            ({"player_age": 2}, "player_age"),
            ({"player_name": "A"}, "player_name"),
            ({"guardian_name": ""}, "guardian_name"),
            ({"for_whom": "friend"}, "for_whom"),
            ({"email": "nope"}, "email"),
            ({"best_time": "midnight"}, "best_time"),
            ({"message": "x" * 501}, "message"),
        ):
            self.assert_error(self.submit(**overrides), "invalid", field=field)
        self.assertFalse(Enquiry.objects.exists())

    def test_the_same_phone_at_most_three_a_day_here(self):
        for _ in range(3):
            self.assertEqual(self.submit().status_code, 201)
        response = self.submit(phone="+91 98765 43210")
        self.assert_error(response, "too_many_enquiries")
        self.assertEqual(
            response.data["message"],
            "You've already sent a few enquiries today. They'll be in touch.",
        )
        self.assertEqual(Enquiry.objects.count(), 3)

        # Older than a day doesn't count.
        Enquiry.objects.update(created_at=timezone.now() - timedelta(hours=25))
        self.assertEqual(self.submit().status_code, 201)

    def test_the_same_phone_at_most_ten_a_day_anywhere(self):
        for i in range(10):
            org = self.make_org(f"Academy {i}", f"academy{i}")
            self.make_enquiry(org=org, phone="+919876543210")
        self.assert_error(self.submit(), "too_many_enquiries")
        self.assertEqual(self.submit(phone="9000000002").status_code, 201)

    def test_the_ip_throttle(self):
        self.assertEqual(EnquirySubmitThrottle.scope, "enquiry_submit")
        self.assertEqual(EnquirySubmitAPIView.throttle_classes, [EnquirySubmitThrottle])

        # The rate comes from settings, so changing it there needs no edit here.
        rate = settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]["enquiry_submit"]
        allowed, window = EnquirySubmitThrottle().parse_rate(rate)
        self.assertEqual(window, 3600)

        # Every attempt counts, a refused one included.
        for _ in range(allowed):
            self.assertNotEqual(self.submit(body={}).status_code, 429)
        # Signing in does not buy a fresh bucket.
        response = self.submit(user=self.p1)
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.data["data"]["code"], "throttled")
