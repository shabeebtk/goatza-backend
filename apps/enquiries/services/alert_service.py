"""
Telling the team about enquiries — app + push for every one, email that never
floods.

IN-APP + PUSH (``NotificationService.enquiry_received``), after commit, for
every enquiry: a row to each active member holding enquiries.manage, and a
push only when that person has no UNREAD enquiry from this org yet — ten
enquiries in an hour are one buzz.

EMAIL, all under the org's EnquiryAlertState row lock, days in Asia/Kolkata:

  * WINDOW = 30 minutes, DAILY_CAP = 8 emails per org per day.
  * A new enquiry: no enquiry email to this org in the last 30 minutes and
    today's count under the cap → one "New enquiry for {org}" email now, and
    ``alert_emailed_at`` is stamped. Otherwise it WAITS (null).
  * Every 10 minutes ``send_digests`` (beat + ``send_enquiry_digests``): per
    org with waiting enquiries that are still NEW and from the last 3 days,
    whose window has passed and that is under the cap → ONE email
    "{n} new enquiries for {org}" listing up to 5, and all of them stamped.
  * Over the cap, waiting ones go in the next day's first digest. Anything
    waiting longer than 3 days is never emailed (the app still has it); an
    enquiry already handled is left out.
  * An org with nobody to email (no one holding enquiries.manage with the
    switch on and an address) spends neither the window nor the cap.

NEVER in an email: a phone number, an age, the message. Name, program,
branch, time and an "Open enquiries" button — the details stay in the app.
"""

import logging
from datetime import timedelta
from zoneinfo import ZoneInfo

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.enquiries.models import Enquiry, EnquiryAlertState
from utils.transactional_emails import (
    enquiry_alert_recipients,
    send_enquiry_alert_email,
    send_enquiry_digest_email,
)

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
WINDOW = timedelta(minutes=30)
DAILY_CAP = 8
DIGEST_MAX_AGE = timedelta(days=3)
DIGEST_LIST_MAX = 5


def ist_date(moment):
    return timezone.localtime(moment, IST).date()


def lock_alert_state(organization):
    """
    The org's EnquiryAlertState row, locked; created on first use in a
    savepoint, so two first enquiries racing both end up locking the one row.
    """
    state = (
        EnquiryAlertState.objects.select_for_update()
        .filter(organization=organization)
        .first()
    )
    if state is not None:
        return state

    try:
        with transaction.atomic():
            EnquiryAlertState.objects.create(organization=organization)
    except IntegrityError:
        pass

    return EnquiryAlertState.objects.select_for_update().get(organization=organization)


def _roll_day(state, now):
    """A new IST day starts the count again."""
    today = ist_date(now)
    if state.emails_day != today:
        state.emails_day = today
        state.emails_today = 0


def may_email(state, now) -> bool:
    """Outside the window and under today's cap. Call ``_roll_day`` first."""
    window_passed = state.last_email_at is None or now - state.last_email_at >= WINDOW
    return window_passed and state.emails_today < DAILY_CAP


def _record_email(state, now):
    state.last_email_at = now
    state.emails_today += 1
    state.save(update_fields=["last_email_at", "emails_day", "emails_today"])


def _notify_in_app(enquiry_id):
    from apps.notifications.services.notification_service import NotificationService

    enquiry = (
        Enquiry.objects.select_related("organization__profile")
        .filter(id=enquiry_id)
        .first()
    )
    if enquiry is not None:
        NotificationService.enquiry_received(enquiry)


def _send_immediate(enquiry_id):
    enquiry = (
        Enquiry.objects.select_related("organization")
        .filter(id=enquiry_id)
        .first()
    )
    if enquiry is not None:
        send_enquiry_alert_email(enquiry)


def on_new_enquiry(enquiry, state, now):
    """
    Inside the submit transaction, with ``state`` locked. Decides the
    immediate email and schedules both alerts for after commit. Lambdas, never
    ``functools.partial`` (robust on_commit logs ``__qualname__``).
    """
    enquiry_id = enquiry.id

    _roll_day(state, now)
    if may_email(state, now) and enquiry_alert_recipients(enquiry.organization):
        enquiry.alert_emailed_at = now
        enquiry.save(update_fields=["alert_emailed_at"])
        _record_email(state, now)
        transaction.on_commit(lambda: _send_immediate(enquiry_id), robust=True)
    else:
        # It waits for a digest. Keep the day's reset if the day rolled.
        state.save(update_fields=["emails_day", "emails_today"])

    transaction.on_commit(lambda: _notify_in_app(enquiry_id), robust=True)


def waiting_enquiries(organization, now):
    return Enquiry.objects.filter(
        organization=organization,
        status=Enquiry.Status.NEW,
        alert_emailed_at__isnull=True,
        created_at__gte=now - DIGEST_MAX_AGE,
    )


def _digest_one(organization, now) -> bool:
    """One org's digest, in its own transaction. True when an email went out."""
    with transaction.atomic():
        state = lock_alert_state(organization)
        _roll_day(state, now)

        if not may_email(state, now):
            state.save(update_fields=["emails_day", "emails_today"])
            return False

        waiting = list(
            waiting_enquiries(organization, now)
            .select_for_update()
            .order_by("-created_at", "-id")
        )
        if not waiting or not enquiry_alert_recipients(organization):
            state.save(update_fields=["emails_day", "emails_today"])
            return False

        Enquiry.objects.filter(id__in=[e.id for e in waiting]).update(
            alert_emailed_at=now
        )
        _record_email(state, now)

        listed_ids = [e.id for e in waiting[:DIGEST_LIST_MAX]]
        total = len(waiting)
        organization_id = organization.id
        transaction.on_commit(
            lambda: _send_digest(organization_id, listed_ids, total), robust=True
        )

    return True


def _send_digest(organization_id, listed_ids, total):
    from apps.organization.models import Organization

    organization = Organization.objects.filter(id=organization_id).first()
    if organization is None:
        return
    listed = list(
        Enquiry.objects.filter(id__in=listed_ids).order_by("-created_at", "-id")
    )
    send_enquiry_digest_email(organization, listed, total)


def send_digests(now=None) -> int:
    """
    Every org whose waiting enquiries are due a digest. Returns how many
    emails went out. Each org is its own transaction, so one failure does not
    stop the rest and a redelivered run finds the stamped ones done.
    """
    from apps.organization.models import Organization

    now = now or timezone.now()
    organization_ids = set(
        Enquiry.objects.filter(
            status=Enquiry.Status.NEW,
            alert_emailed_at__isnull=True,
            created_at__gte=now - DIGEST_MAX_AGE,
        ).values_list("organization_id", flat=True)
    )

    sent = 0
    # A suspended or deactivated org's team is not emailed; its enquiries
    # stay in the inbox.
    organizations = Organization.objects.filter(
        id__in=organization_ids, is_active=True, is_suspended=False
    )
    for organization in organizations:
        try:
            if _digest_one(organization, now):
                sent += 1
        except Exception:
            logger.error(
                "enquiries | digest failed | org=%s", organization.id, exc_info=True
            )
    return sent
