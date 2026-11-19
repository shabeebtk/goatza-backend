"""
The page's payloads. AN ALLOW-LIST: every key is written out here, nothing is
a model field passed through. The keys are a contract with the frontend.

The editor and the public page share the program and location shapes, so a
program looks the same in the club's list and on the page it publishes.
"""

from apps.orgpages.models import Coach, FacilityItem, PageSettings, Program


def time_label(value):
    """``datetime.time`` → ``"16:30"``."""
    return value.strftime("%H:%M") if value else None


def location_mini(location):
    if location is None:
        return None
    return {"id": str(location.id), "name": location.name, "city": location.city}


def contact_payload(page):
    """The editor's contact block: every switch and every stored value."""
    return {
        "enquiry_form_enabled": page.enquiry_form_enabled,
        "whatsapp_enabled": page.whatsapp_enabled,
        "whatsapp_number": page.whatsapp_number,
        "call_enabled": page.call_enabled,
        "call_number": page.call_number,
        "email_enabled": page.email_enabled,
        "contact_email": page.contact_email,
    }


def facts_payload(page):
    """The editor's facts block, as stored."""
    return {
        "founded_year": page.founded_year,
        "age_min": page.age_min,
        "age_max": page.age_max,
        "coaching_languages": list(page.coaching_languages or []),
        "training_days": list(page.training_days or []),
        "student_range": page.student_range,
        "student_range_label": (
            page.get_student_range_display() if page.student_range else ""
        ),
        "instagram_url": page.instagram_url,
        "youtube_url": page.youtube_url,
        "facebook_url": page.facebook_url,
    }


def settings_payload(page: PageSettings):
    return {
        "contact": contact_payload(page),
        "facts": facts_payload(page),
        "sections": page.section_switches(),
    }


def program_payload(program: Program):
    return {
        "id": str(program.id),
        "name": program.name,
        "sport": {"id": str(program.sport_id), "name": program.sport.name},
        "age_min": program.age_min,
        "age_max": program.age_max,
        "level": program.level,
        "level_label": program.get_level_display(),
        "days": list(program.days or []),
        "start_time": time_label(program.start_time),
        "end_time": time_label(program.end_time),
        "location": location_mini(program.location),
        "fee": {
            "mode": program.fee_mode,
            "amount": program.fee_amount,
            "currency": program.currency,
            "per": program.fee_per,
        },
        "free_trial": program.free_trial,
        "spots": program.spots,
        "spots_label": program.get_spots_display(),
    }


def catalog_payload(items):
    """The active catalog, grouped in Group order: ``[{group, label, items: [{key, label}]}]``."""
    by_group = {}
    for item in items:
        by_group.setdefault(item.group, []).append(
            {"key": item.key, "label": item.label}
        )
    return [
        {"group": group, "label": label, "items": by_group[group]}
        for group, label in FacilityItem.Group.choices
        if group in by_group
    ]


def coach_role_label(coach: Coach):
    if coach.role == Coach.Role.OTHER and coach.title:
        return coach.title
    return coach.get_role_display()


def coach_person(coach: Coach):
    """``(name, username, photo)`` — a member coach's are their profile's."""
    if coach.member_id:
        user = coach.member.user
        profile = getattr(user, "profile", None)
        return (
            getattr(profile, "name", "") or user.username or "",
            user.username or "",
            getattr(profile, "profile_photo", "") or "",
        )
    return coach.name, None, coach.photo_url


def coach_editor_payload(coach: Coach):
    name, username, photo = coach_person(coach)
    return {
        "id": str(coach.id),
        "is_member": coach.member_id is not None,
        "member_id": str(coach.member_id) if coach.member_id else None,
        "name": name,
        "username": username,
        "photo": photo,
        "role": coach.role,
        "role_label": coach_role_label(coach),
        "title": coach.title,
        "licence": coach.licence,
        "years_coaching": coach.years_coaching,
        "is_visible": coach.is_visible,
        "is_hidden": not coach.is_visible,
        "order": coach.order,
    }


def coach_candidate_payload(member):
    user = member.user
    profile = getattr(user, "profile", None)
    return {
        "member_id": str(member.id),
        "name": getattr(profile, "name", "") or user.username or "",
        "username": user.username or "",
        "photo": getattr(profile, "profile_photo", "") or "",
        "role": member.role,
    }


def facility_photo_payload(photo):
    return {"id": str(photo.id), "url": photo.photo_url, "order": photo.order}

