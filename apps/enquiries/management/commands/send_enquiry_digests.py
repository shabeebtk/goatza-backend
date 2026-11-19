"""
Send the waiting enquiry digests — ONE email per org whose waiting enquiries
are due one (apps/enquiries/services/alert_service.py has the rule: still new,
from the last 3 days, the org's 30-minute window passed, under its daily cap
of 8).

Beat runs it every 10 minutes (``enquiries.send_digests``). Run it by hand:

    python manage.py send_enquiry_digests

SAFE TO RUN TWICE: each org's digest stamps every enquiry it covers under the
org's row lock, so a second run finds nothing waiting — or finds the window
shut by the email the first one just sent.
"""

from django.core.management.base import BaseCommand

from apps.enquiries.services.alert_service import send_digests


class Command(BaseCommand):
    help = "Email each organization's waiting enquiries as one digest (every 10 minutes)."

    def handle(self, *args, **options):
        sent = send_digests()
        self.stdout.write(self.style.SUCCESS(f"Enquiry digests sent: {sent}"))
