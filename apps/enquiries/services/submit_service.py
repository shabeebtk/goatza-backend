"""
The public enquiry form: POST /enquiries/submit/<org_username>. No account
needed; a signed-in sender is recorded, never required.

THE ORDER, and why:

  1. The org — active, not suspended, its form switched on, no block between a
     signed-in sender and it. Every miss is the SAME 404, which never says why.
  2. The honeypot. ``website`` filled in → answered exactly like a success
     (201) and nothing is stored, nobody is told.
  3. The fields. An under-18 asking for themselves is ``guardian_needed`` —
     a minor's own contact is never stored.
  4. Under the org's EnquiryAlertState row lock: the same phone at this org
     at most 3 times in 24 h, and across all orgs at most 10, else
     ``too_many_enquiries``. Then the row, the email decision and the in-app
     alerts (alert_service).

The coarse limit is the 30/hour per-IP throttle on the view.
"""

import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.enquiries.errors import (
    form_not_found,
    guardian_needed,
    too_many_enquiries,
)
from apps.enquiries.models import MESSAGE_MAX, PLAYER_NAME_MAX, Enquiry
from apps.enquiries.services.alert_service import lock_alert_state, on_new_enquiry
from apps.moderation.services.block_guard import is_blocked
from apps.organization.models import Organization, OrganizationLocation
from apps.orgpages.errors import invalid
from apps.orgpages.models import Program
from apps.orgpages.phones import InvalidPhone, mask_phone, normalize_phone
from apps.orgpages.services.cleaning import (
    clean_bool,
    clean_choice,
    clean_email,
    clean_int,
    clean_text,
)
from apps.orgpages.services.guards import page_settings_for
from utils.validations import is_valid_uuid

logger = logging.getLogger(__name__)

ADULT_AGE = 18
AGE_MIN = 3
AGE_MAX = 80
NAME_MIN = 2

PER_ORG_LIMIT = 3
OVERALL_LIMIT = 10
LIMIT_WINDOW = timedelta(hours=24)


def form_organization(username, user, actor):
    """The org whose form this sender may use, or the one 404."""
    organization = (
        Organization.objects
        .filter(username=username, is_active=True, is_suspended=False)
        .select_related("profile")
        .first()
        if isinstance(username, str) and username else None
    )
    if organization is None:
        raise form_not_found()

    if not page_settings_for(organization).enquiry_form_enabled:
        raise form_not_found()

    if user is not None and is_blocked(user, organization):
        raise form_not_found()
    if actor is not None and actor.is_org and is_blocked(actor, organization):
        raise form_not_found()

    return organization


def _org_program(organization, value):
    if value in (None, ""):
        return None
    program = (
        Program.objects.filter(id=value, organization=organization)
        .select_related("location")
        .first()
        if is_valid_uuid(str(value)) else None
    )
    if program is None:
        raise invalid("Choose one of this page's programs.", "program_id")
    return program


def _org_location(organization, value):
    if value in (None, ""):
        return None
    location = (
        OrganizationLocation.objects.filter(id=value, organization=organization).first()
        if is_valid_uuid(str(value)) else None
    )
    if location is None:
        raise invalid("Choose one of this page's locations.", "location_id")
    return location


def location_label(location):
    if location is None:
        return ""
    return location.name or location.city or ""


def _clean(organization, payload):
    for_whom = payload.get("for_whom")
    if for_whom not in Enquiry.ForWhom.values:
        raise invalid("Choose who the enquiry is for.", "for_whom")
    player_name = clean_text(
        payload.get("player_name"), "player_name", label="Name",
        min_len=NAME_MIN, max_len=PLAYER_NAME_MAX, required=True,
    )
    player_age = clean_int(
        payload.get("player_age"), "player_age", label="Age", lo=AGE_MIN, hi=AGE_MAX
    )

    # Before anything about how to reach them is even read.
    if for_whom == Enquiry.ForWhom.SELF and player_age < ADULT_AGE:
        raise guardian_needed()

    guardian_name = ""
    if for_whom == Enquiry.ForWhom.CHILD:
        guardian_name = clean_text(
            payload.get("guardian_name"), "guardian_name", label="Your name",
            min_len=NAME_MIN, max_len=PLAYER_NAME_MAX, required=True,
        )

    raw_phone = payload.get("phone")
    if raw_phone in (None, ""):
        raise invalid("Enter a phone number.", "phone")
    try:
        phone = normalize_phone(raw_phone)
    except InvalidPhone as exc:
        raise invalid(str(exc), "phone")

    phone_is_whatsapp = False
    if payload.get("phone_is_whatsapp") is not None:
        phone_is_whatsapp = clean_bool(payload["phone_is_whatsapp"], "phone_is_whatsapp")

    email = clean_email(payload.get("email"), "email")

    program = _org_program(organization, payload.get("program_id"))
    location = _org_location(organization, payload.get("location_id"))
    if location is None and program is not None:
        location = program.location

    best_time = clean_choice(
        payload.get("best_time"), "best_time", label="Best time",
        choices=Enquiry.BestTime.values, allow_blank=True,
    )
    message = clean_text(
        payload.get("message"), "message", label="Message", max_len=MESSAGE_MAX
    )

    return {
        "for_whom": for_whom,
        "player_name": player_name,
        "player_age": player_age,
        "guardian_name": guardian_name,
        "phone": phone,
        "phone_is_whatsapp": phone_is_whatsapp,
        "email": email,
        "program": program,
        "program_name": program.name if program else "",
        "location": location,
        "location_name": location_label(location),
        "best_time": best_time,
        "message": message,
    }


def _decoy_answer(organization, payload):
    """
    What a success would have said, built without storing anything — for a
    filled honeypot. Best effort: a value that would have been refused just
    reads as empty.
    """
    try:
        phone = normalize_phone(payload.get("phone"))
    except InvalidPhone:
        phone = ""

    program = location = None
    try:
        program = _org_program(organization, payload.get("program_id"))
        location = _org_location(organization, payload.get("location_id"))
    except Exception:
        pass
    if location is None and program is not None:
        location = program.location

    return {
        "program_name": program.name if program else "",
        "location_name": location_label(location),
        "phone_masked": mask_phone(phone),
    }


def answer_for(enquiry):
    """Just enough for the "Enquiry sent" screen. The full phone is never echoed."""
    return {
        "program_name": enquiry.program_name,
        "location_name": enquiry.location_name,
        "phone_masked": mask_phone(enquiry.phone),
    }


class EnquirySubmitService:

    @staticmethod
    def submit(username, payload, *, user=None, actor=None):
        """``{program_name, location_name, phone_masked}`` for the 201."""
        payload = payload if isinstance(payload, dict) else {}
        organization = form_organization(username, user, actor)

        honeypot = payload.get("website")
        if honeypot not in (None, "") and str(honeypot).strip():
            logger.info("[ENQUIRY] honeypot | org=%s", organization.id)
            return _decoy_answer(organization, payload)

        fields = _clean(organization, payload)
        phone = fields["phone"]

        with transaction.atomic():
            state = lock_alert_state(organization)
            now = timezone.now()
            since = now - LIMIT_WINDOW

            recent = Enquiry.objects.filter(phone=phone, created_at__gte=since)
            if (
                recent.filter(organization=organization).count() >= PER_ORG_LIMIT
                or recent.count() >= OVERALL_LIMIT
            ):
                raise too_many_enquiries()

            enquiry = Enquiry.objects.create(
                organization=organization,
                submitted_by=user,
                **fields,
            )
            on_new_enquiry(enquiry, state, now)

        logger.info("[ENQUIRY] received | enquiry=%s | org=%s", enquiry.id, organization.id)
        return answer_for(enquiry)
