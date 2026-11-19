"""
The inbox payloads. AN ALLOW-LIST: every key is written out. These go only to
members holding enquiries.manage — never to the sender, never to the public.
"""

INBOX_MESSAGE_PREVIEW = 120


def _member_name(member):
    if member is None:
        return None
    user = member.user
    profile = getattr(user, "profile", None)
    return getattr(profile, "name", "") or user.username or ""


def _iso(value):
    return value.isoformat() if value else None


def inbox_item(enquiry):
    return {
        "id": str(enquiry.id),
        "status": enquiry.status,
        "for_whom": enquiry.for_whom,
        "player_name": enquiry.player_name,
        "player_age": enquiry.player_age,
        "guardian_name": enquiry.guardian_name,
        "phone": enquiry.phone,
        "phone_is_whatsapp": enquiry.phone_is_whatsapp,
        "program_name": enquiry.program_name,
        "location_name": enquiry.location_name,
        "best_time": enquiry.best_time,
        "message": (enquiry.message or "")[:INBOX_MESSAGE_PREVIEW],
        "created_at": _iso(enquiry.created_at),
    }


def enquiry_detail(enquiry):
    return {
        **inbox_item(enquiry),
        "message": enquiry.message,
        "email": enquiry.email,
        "program_id": str(enquiry.program_id) if enquiry.program_id else None,
        "location_id": str(enquiry.location_id) if enquiry.location_id else None,
        "status_label": enquiry.get_status_display(),
        "best_time_label": enquiry.get_best_time_display() if enquiry.best_time else "",
        "notes": enquiry.notes,
        "notes_updated_by": _member_name(enquiry.notes_updated_by),
        "notes_updated_at": _iso(enquiry.notes_updated_at),
        "status_changed_by": _member_name(enquiry.status_changed_by),
        "status_changed_at": _iso(enquiry.status_changed_at),
        "closed_at": _iso(enquiry.closed_at),
    }
