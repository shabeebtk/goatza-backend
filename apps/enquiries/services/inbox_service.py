"""
Working an enquiry: its status and the team's notes. ``enquiries.manage``.

PATCH records who and when for each part that changed. ``closed_at`` is set
when the status becomes joined or not_interested and cleared when it goes
back to new or contacted — retention counts from it.
"""

from django.db import transaction
from django.utils import timezone

from apps.enquiries.errors import EnquiryError, enquiry_not_found
from apps.enquiries.models import NOTES_MAX, Enquiry
from apps.organization.permissions import OrgPermission
from apps.orgpages.errors import invalid
from apps.orgpages.services.cleaning import clean_text
from apps.organization.models import OrganizationMember
from apps.orgpages.services.guards import relock_actor, require_page_permission
from utils.validations import is_valid_uuid

MANAGE = OrgPermission.ENQUIRIES_MANAGE
SWITCH_TO_PERSONAL_MESSAGE = "Switch to your personal account to do this."
ACTOR_LOST_MESSAGE = "You can no longer handle this organization's enquiries."


def require_inbox(actor):
    """``(organization, acting member)`` holding enquiries.manage, or 403."""
    return require_page_permission(actor, MANAGE)


class EnquiryInboxService:

    @staticmethod
    def update(actor, enquiry_id, payload):
        organization, member = require_inbox(actor)
        payload = payload if isinstance(payload, dict) else {}

        status = None
        if "status" in payload:
            status = payload.get("status")
            if status not in Enquiry.Status.values:
                raise invalid("Choose a valid status.", "status")
        notes = None
        if "notes" in payload:
            notes = clean_text(payload.get("notes"), "notes", label="Notes", max_len=NOTES_MAX)

        with transaction.atomic():
            enquiry = (
                Enquiry.objects.select_for_update()
                .filter(id=enquiry_id, organization=organization)
                .first()
                if is_valid_uuid(str(enquiry_id)) else None
            )
            if enquiry is None:
                raise enquiry_not_found()
            member = relock_actor(member, MANAGE, message=ACTOR_LOST_MESSAGE)

            now = timezone.now()
            fields = []

            if status is not None and status != enquiry.status:
                enquiry.status = status
                enquiry.status_changed_at = now
                enquiry.status_changed_by = member
                enquiry.closed_at = now if status in Enquiry.CLOSED_STATUSES else None
                fields += ["status", "status_changed_at", "status_changed_by", "closed_at"]

            if notes is not None and notes != enquiry.notes:
                enquiry.notes = notes
                enquiry.notes_updated_at = now
                enquiry.notes_updated_by = member
                fields += ["notes", "notes_updated_at", "notes_updated_by"]

            if fields:
                enquiry.save(update_fields=[*fields, "updated_at"])

        return enquiry


def _handler_membership(user, organization_id):
    """The person's ACTIVE membership of that org holding enquiries.manage, or 404."""
    from apps.organization.permissions import has_org_permission

    member = (
        OrganizationMember.objects
        .filter(organization_id=organization_id, user=user)
        .first()
        if user is not None and is_valid_uuid(str(organization_id)) else None
    )
    if member is None or not has_org_permission(member, MANAGE):
        raise EnquiryError(
            "not_found", "This organization wasn't found.", status_code=404,
            field="org_id",
        )
    return member


def _require_person(actor):
    if actor is None or not actor.is_user or actor.user is None:
        raise EnquiryError(
            "switch_to_personal", SWITCH_TO_PERSONAL_MESSAGE, status_code=403
        )
    return actor.user


class EnquiryAlertPreferenceService:
    """The person's OWN email switch, per org. Never the org's to set."""

    @staticmethod
    def get(actor, organization_id):
        user = _require_person(actor)
        return _handler_membership(user, organization_id).enquiry_email_alerts

    @staticmethod
    def set(actor, payload):
        user = _require_person(actor)
        payload = payload if isinstance(payload, dict) else {}
        email = payload.get("email")
        if not isinstance(email, bool):
            raise invalid("Choose on or off.", "email")

        with transaction.atomic():
            member = _handler_membership(user, payload.get("org_id"))
            OrganizationMember.objects.filter(id=member.id).update(
                enquiry_email_alerts=email
            )
        return email
