"""Shared fixtures for the enquiry tests: the org page fixtures plus a program."""

import datetime

from apps.enquiries.models import Enquiry
from apps.notifications.tasks import push_notification
from apps.orgpages.tests.base import PageTestBase


class EnquiryTestBase(PageTestBase):

    def setUp(self):
        super().setUp()
        self.program = self.make_program(
            "U12 Football", location=self.branch, start_time=datetime.time(16, 30),
            end_time=datetime.time(18, 0),
        )

    def body(self, **overrides):
        body = {
            "for_whom": "child",
            "player_name": "Aarav Menon",
            "player_age": 9,
            "guardian_name": "Priya Menon",
            "phone": "98765 43210",
            "phone_is_whatsapp": True,
            "program_id": str(self.program.id),
            "best_time": "evening",
            "message": "Is there a trial class this weekend?",
        }
        body.update(overrides)
        return body

    def submit(self, body=None, user=None, username=None, **overrides):
        self.client.force_authenticate(user=user)
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(
                f"/enquiries/submit/{username or self.org.username}",
                body if body is not None else self.body(**overrides),
                format="json",
            )

    def make_enquiry(self, org=None, **fields):
        fields.setdefault("for_whom", Enquiry.ForWhom.CHILD)
        fields.setdefault("player_name", "Kid")
        fields.setdefault("player_age", 10)
        fields.setdefault("guardian_name", "Parent")
        fields.setdefault("phone", "+919000000001")
        return Enquiry.objects.create(organization=org or self.org, **fields)

    def inbox_get(self, user, path="inbox", org=None, **params):
        return self.client.get(f"/enquiries/{path}", params, **self._org_headers(user, org))

    def inbox_patch(self, user, enquiry_id, data, org=None):
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.patch(
                f"/enquiries/{enquiry_id}", data, format="json",
                **self._org_headers(user, org),
            )

    def pushes(self):
        """How many pushes were handed to the worker."""
        return sum(
            1 for call in self.push.call_args_list if call.args and call.args[0] is push_notification
        )

    def sent_emails(self):
        """``[(subject, text, html, to)]`` handed to the mail sender."""
        return [
            (
                call.kwargs["subject"],
                call.kwargs["message"],
                call.kwargs.get("html_message") or "",
                call.kwargs["to_email"],
            )
            for call in self.emails.call_args_list
        ]
