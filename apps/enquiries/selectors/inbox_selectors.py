"""
The org's enquiry inbox — reads only, behind ``enquiries.manage``.

Newest first, 20 a page, by an opaque cursor over ``(created_at, id)``: a page
never repeats or skips a row when new enquiries arrive while someone scrolls.
The ``counts`` follow the program / location filters but never the status
filter, so the tabs say how many are in each.
"""

import base64
import binascii
import json
from datetime import datetime

from django.db.models import Count, Q

from apps.enquiries.models import Enquiry
from apps.orgpages.errors import invalid
from utils.validations import is_valid_uuid

PAGE_SIZE = 20
BAD_CURSOR = "This list changed. Refresh and try again."


def encode_cursor(enquiry):
    raw = json.dumps([enquiry.created_at.isoformat(), str(enquiry.id)])
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(value):
    """``(created_at, id)``, or 400 invalid on ``cursor``."""
    try:
        padded = value + "=" * (-len(value) % 4)
        created_at, enquiry_id = json.loads(base64.urlsafe_b64decode(padded))
        created_at = datetime.fromisoformat(created_at)
    except (ValueError, TypeError, binascii.Error, json.JSONDecodeError):
        raise invalid(BAD_CURSOR, "cursor")
    if created_at.tzinfo is None or not is_valid_uuid(str(enquiry_id)):
        raise invalid(BAD_CURSOR, "cursor")
    return created_at, enquiry_id


def _uuid_param(params, field):
    value = (params.get(field) or "").strip()
    if not value:
        return None
    if not is_valid_uuid(value):
        raise invalid("Choose a valid filter.", field)
    return value


def inbox_page(organization, params):
    """``{items: [Enquiry], next_cursor, counts}`` for GET /enquiries/inbox."""
    status = (params.get("status") or "").strip()
    if status and status not in Enquiry.Status.values:
        raise invalid("Choose a valid status.", "status")

    filtered = Enquiry.objects.filter(organization=organization)
    program_id = _uuid_param(params, "program_id")
    if program_id:
        filtered = filtered.filter(program_id=program_id)
    location_id = _uuid_param(params, "location_id")
    if location_id:
        filtered = filtered.filter(location_id=location_id)

    counts = filtered.aggregate(
        **{
            value: Count("id", filter=Q(status=value))
            for value in Enquiry.Status.values
        }
    )

    rows = filtered.filter(status=status) if status else filtered
    cursor = (params.get("cursor") or "").strip()
    if cursor:
        created_at, enquiry_id = decode_cursor(cursor)
        rows = rows.filter(
            Q(created_at__lt=created_at)
            | Q(created_at=created_at, id__lt=enquiry_id)
        )

    page = list(rows.order_by("-created_at", "-id")[: PAGE_SIZE + 1])
    has_more = len(page) > PAGE_SIZE
    page = page[:PAGE_SIZE]

    return {
        "items": page,
        "next_cursor": encode_cursor(page[-1]) if has_more and page else None,
        "counts": counts,
    }


def unread_count(organization):
    return Enquiry.objects.filter(
        organization=organization, status=Enquiry.Status.NEW
    ).count()


def org_enquiry(organization, enquiry_id):
    """One enquiry of this org, or None. Another org's reads like a missing one."""
    if not is_valid_uuid(str(enquiry_id)):
        return None
    return (
        Enquiry.objects
        .filter(id=enquiry_id, organization=organization)
        .select_related(
            "notes_updated_by__user__profile",
            "status_changed_by__user__profile",
        )
        .first()
    )
