"""Retention: closed enquiries go after 365 days; a purged account's go with it."""

import io
from datetime import timedelta

from django.core.management import call_command
from django.utils import timezone

from apps.enquiries.models import Enquiry
from apps.enquiries.tests.base import EnquiryTestBase


class RetentionTests(EnquiryTestBase):

    def test_the_purge_deletes_closed_over_365_days_only(self):
        now = timezone.now()
        old_closed = self.make_enquiry(status=Enquiry.Status.JOINED, closed_at=now - timedelta(days=366))
        old_declined = self.make_enquiry(
            status=Enquiry.Status.NOT_INTERESTED, closed_at=now - timedelta(days=400)
        )
        recent_closed = self.make_enquiry(status=Enquiry.Status.JOINED, closed_at=now - timedelta(days=30))
        old_open = self.make_enquiry(status=Enquiry.Status.CONTACTED)
        Enquiry.objects.filter(id=old_open.id).update(created_at=now - timedelta(days=800))

        call_command("purge_closed_enquiries", "--dry-run", stdout=io.StringIO())
        self.assertEqual(Enquiry.objects.count(), 4)

        call_command("purge_closed_enquiries", stdout=io.StringIO())
        remaining = set(Enquiry.objects.values_list("id", flat=True))
        self.assertEqual(remaining, {recent_closed.id, old_open.id})
        self.assertNotIn(old_closed.id, remaining)
        self.assertNotIn(old_declined.id, remaining)

    def test_the_account_purge_deletes_what_the_user_sent(self):
        from apps.accounts.management.commands.purge_deleted_accounts import Command

        self.submit(user=self.p1, for_whom="self", player_age=30)
        self.submit(phone="9000000002")
        self.assertEqual(Enquiry.objects.count(), 2)

        with self.captureOnCommitCallbacks(execute=True):
            Command()._purge(self.p1)

        self.assertFalse(Enquiry.objects.filter(submitted_by=self.p1).exists())
        self.assertEqual(Enquiry.objects.count(), 1)

    def test_an_orgs_enquiries_cascade_with_it(self):
        self.make_enquiry()
        self.org.delete()
        self.assertFalse(Enquiry.objects.exists())
