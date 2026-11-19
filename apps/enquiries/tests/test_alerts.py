"""App + push for every enquiry; email that never floods."""

from datetime import datetime, time, timedelta

from django.utils import timezone

from apps.enquiries.models import Enquiry, EnquiryAlertState
from apps.enquiries.services.alert_service import DAILY_CAP, IST, ist_date, send_digests
from apps.enquiries.tests.base import EnquiryTestBase
from apps.notifications.models import Notification
from apps.notifications.services.notification_service import build_notification_payload
from apps.organization.models import OrganizationMember


class InAppAlertTests(EnquiryTestBase):

    def test_owner_and_staff_are_told_and_a_coach_is_not(self):
        self.submit()
        recipients = set(
            Notification.objects.filter(type=Notification.Type.ENQUIRY_RECEIVED)
            .values_list("recipient_user_id", flat=True)
        )
        self.assertEqual(recipients, {self.owner.id, self.staff.id})

        notification = Notification.objects.filter(recipient_user=self.owner).get()
        self.assertEqual(notification.actor_org_id, self.org.id)
        payload = build_notification_payload(notification)
        self.assertEqual(payload["title"], "New enquiry")
        self.assertEqual(payload["body"], "Aarav, 9 · U12 Football")
        enquiry = Enquiry.objects.get()
        self.assertEqual(
            payload["url"], f"/organization/admin/{self.org.id}/enquiries/{enquiry.id}"
        )

    def test_a_general_enquirys_copy(self):
        self.submit(program_id=None)
        notification = Notification.objects.filter(recipient_user=self.owner).get()
        self.assertEqual(build_notification_payload(notification)["body"], "Aarav, 9 · General enquiry")

    def test_push_is_held_while_an_unread_one_waits(self):
        self.submit()
        self.assertEqual(self.pushes(), 2)

        self.submit(phone="9000000002")
        self.assertEqual(self.pushes(), 2)
        self.assertEqual(Notification.objects.filter(recipient_user=self.owner).count(), 2)

        Notification.objects.filter(recipient_user=self.owner).update(is_read=True)
        self.submit(phone="9000000003")
        self.assertEqual(self.pushes(), 3)


class EmailAlertTests(EnquiryTestBase):

    def state(self):
        return EnquiryAlertState.objects.get(organization=self.org)

    def test_the_first_emails_at_once_and_a_second_inside_30_minutes_waits(self):
        self.submit()
        emails = self.sent_emails()
        self.assertEqual(len(emails), 1)
        subject, _, _, to = emails[0]
        self.assertEqual(subject, "New enquiry for Bright Academy")
        self.assertEqual(to, ["pageowner@example.com", "pagestaff@example.com"])
        self.assertIsNotNone(Enquiry.objects.get().alert_emailed_at)

        self.submit(phone="9000000002")
        self.assertEqual(len(self.sent_emails()), 1)
        waiting = Enquiry.objects.filter(alert_emailed_at__isnull=True)
        self.assertEqual(waiting.count(), 1)
        self.assertEqual(self.state().emails_today, 1)

    def test_the_digest_sends_one_email_for_the_waiting_and_skips_a_handled_one(self):
        self.submit()
        self.submit(phone="9000000002", player_name="Diya Nair")
        self.submit(phone="9000000003", player_name="Ishaan Rao")
        handled = Enquiry.objects.get(player_name="Ishaan Rao")
        Enquiry.objects.filter(id=handled.id).update(status=Enquiry.Status.CONTACTED)

        # Inside the window: nothing.
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(send_digests(), 0)
        self.assertEqual(len(self.sent_emails()), 1)

        later = timezone.now() + timedelta(minutes=31)
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(send_digests(now=later), 1)

        emails = self.sent_emails()
        self.assertEqual(len(emails), 2)
        subject, text, html, _ = emails[1]
        self.assertEqual(subject, "1 new enquiry for Bright Academy")
        self.assertIn("Diya", text)
        self.assertNotIn("Ishaan", text + html)
        self.assertIsNotNone(Enquiry.objects.get(player_name="Diya Nair").alert_emailed_at)
        self.assertIsNone(Enquiry.objects.get(id=handled.id).alert_emailed_at)

        # Run again: nothing is waiting.
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(send_digests(now=later + timedelta(minutes=40)), 0)

    def test_a_digest_lists_five_and_counts_the_rest(self):
        # Noon IST, so "31 minutes later" is always the same day: run near
        # midnight, the count would rightly start again.
        noon = datetime.combine(ist_date(timezone.now()), time(12, 0), tzinfo=IST)
        state = EnquiryAlertState.objects.create(
            organization=self.org, last_email_at=noon,
            emails_day=ist_date(noon), emails_today=1,
        )
        for i in range(7):
            self.make_enquiry(player_name=f"Kid{i}", phone=f"+91900000001{i}")
        with self.captureOnCommitCallbacks(execute=True):
            send_digests(now=noon + timedelta(minutes=31))
        subject, text, html, _ = self.sent_emails()[0]
        self.assertEqual(subject, "7 new enquiries for Bright Academy")
        self.assertIn("and 2 more", text)
        state.refresh_from_db()
        self.assertEqual(state.emails_today, 2)

    def test_the_daily_cap_holds_and_the_next_day_digest_catches_up(self):
        now = timezone.now()
        EnquiryAlertState.objects.create(
            organization=self.org, last_email_at=now - timedelta(hours=2),
            emails_day=ist_date(now), emails_today=DAILY_CAP,
        )
        self.submit()
        self.assertEqual(self.sent_emails(), [])

        # Same instant, same IST day: the window has passed, the cap holds.
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(send_digests(now=now), 0)
        self.assertEqual(self.sent_emails(), [])

        tomorrow = now + timedelta(days=1)
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(send_digests(now=tomorrow), 1)
        self.assertEqual(len(self.sent_emails()), 1)
        self.assertEqual(self.state().emails_today, 1)
        self.assertEqual(self.state().emails_day, ist_date(tomorrow))

    def test_waiting_longer_than_three_days_never_emails(self):
        enquiry = self.make_enquiry()
        Enquiry.objects.filter(id=enquiry.id).update(created_at=timezone.now() - timedelta(days=4))
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(send_digests(), 0)
        self.assertEqual(self.sent_emails(), [])

    def test_a_member_who_switched_emails_off_gets_none(self):
        OrganizationMember.objects.filter(id=self.staff_member.id).update(enquiry_email_alerts=False)
        self.submit()
        self.assertEqual(self.sent_emails()[0][3], ["pageowner@example.com"])

        OrganizationMember.objects.filter(id=self.owner_member.id).update(enquiry_email_alerts=False)
        later = timezone.now() + timedelta(minutes=31)
        self.submit(phone="9000000002")
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(send_digests(now=later), 0)
        self.assertEqual(len(self.sent_emails()), 1)

    def test_no_phone_age_or_message_in_any_email(self):
        body = self.body(
            for_whom="self", player_age=71, guardian_name="", player_name="Thomas Kurian",
            message="Please call after six only",
        )
        self.submit(body=body)
        self.submit(body={**body, "phone": "9000000002", "player_name": "Mary Kurian"})
        with self.captureOnCommitCallbacks(execute=True):
            send_digests(now=timezone.now() + timedelta(minutes=31))

        emails = self.sent_emails()
        self.assertEqual(len(emails), 2)
        for subject, text, html, _ in emails:
            everything = subject + text + html
            # The age as the push writes it, or labelled. A bare "71" could be
            # part of an id inside a link.
            for secret in (
                "9876543210", "+919876543210", "9000000002", "98•••• 3210",
                ", 71", "71 ·", "71 years", "Age", "Please call after six only",
                "Kurian",
            ):
                self.assertNotIn(secret, everything)
            self.assertIn("Open enquiries", html)
            self.assertIn("/settings/notifications?org=", html)
            self.assertIn("You get these because you handle enquiries for Bright Academy.", html)


class AlertPreferenceTests(EnquiryTestBase):

    def get(self, user, org_id):
        self.client.force_authenticate(user=user)
        return self.client.get("/enquiries/alerts", {"org_id": str(org_id)})

    def patch(self, user, body):
        self.client.force_authenticate(user=user)
        return self.client.patch("/enquiries/alerts", body, format="json")

    def test_the_person_sets_their_own_switch(self):
        self.assertEqual(self.get(self.staff, self.org.id).data["data"], {"email": True})
        response = self.patch(self.staff, {"org_id": str(self.org.id), "email": False})
        self.assertEqual(response.data["data"], {"email": False})
        self.staff_member.refresh_from_db()
        self.assertFalse(self.staff_member.enquiry_email_alerts)
        # The owner's own switch is untouched.
        self.owner_member.refresh_from_db()
        self.assertTrue(self.owner_member.enquiry_email_alerts)

    def test_anyone_without_enquiries_manage_is_a_404(self):
        self.assert_error(self.get(self.coach, self.org.id), "not_found", 404)
        self.assert_error(self.get(self.p1, self.org.id), "not_found", 404)
        self.assert_error(
            self.patch(self.coach, {"org_id": str(self.org.id), "email": False}), "not_found", 404
        )
        self.assert_error(
            self.patch(self.staff, {"org_id": str(self.org.id), "email": "no"}), "invalid", field="email"
        )

    def test_as_the_org_is_refused(self):
        response = self.client.get(
            "/enquiries/alerts", {"org_id": str(self.org.id)}, **self._org_headers(self.staff)
        )
        self.assert_error(response, "switch_to_personal", 403)
