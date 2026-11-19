# enquiries/tasks.py
"""
Celery tasks for enquiries — THIN, per CLAUDE.md ("Background jobs"): each is
a ``call_command`` of the management command that does the work, so the same
code runs from beat or from a shell.
"""

import logging

from celery import shared_task
from django.core.management import call_command

logger = logging.getLogger(__name__)


@shared_task(name="enquiries.send_digests", acks_late=True)
def send_digests():
    """
    Email each org's waiting enquiries as one digest. Every 10 minutes.

    ``acks_late=True`` although it sends email, because a redelivery CANNOT
    send twice: each org's digest stamps ``alert_emailed_at`` on every
    enquiry it covers, and moves the org's ``last_email_at``, in the same
    transaction, under the org's EnquiryAlertState row lock — a second run
    finds nothing waiting, or the 30-minute window shut.
    """
    call_command("send_enquiry_digests")


@shared_task(name="enquiries.purge_closed_enquiries", acks_late=True)
def purge_closed_enquiries():
    """
    Delete enquiries closed more than 365 days ago. Daily, 03:45 IST.

    SAFE TO RUN TWICE: it selects on ``closed_at`` past the cut-off, so a
    redelivery finds the rows already gone.
    """
    call_command("purge_closed_enquiries")
