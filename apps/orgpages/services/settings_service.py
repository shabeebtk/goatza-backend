"""
The page's own settings: contact options, key facts, section switches. One
row per org (PageSettings), and that row is also the page's lock.

All three are partial PATCHes: only the keys sent are touched, and the rules
that span fields are checked on the MERGED result, so turning WhatsApp on in
one request and adding its number in the next is refused at the first step,
not stored half-done.
"""

import logging

from django.db import transaction
from django.utils import timezone

from apps.orgpages import constants as C
from apps.orgpages.errors import invalid
from apps.orgpages.models import PageSettings
from apps.orgpages.services.cleaning import (
    clean_bool,
    clean_choice,
    clean_days,
    clean_email,
    clean_int,
    clean_phone,
    clean_social_url,
    clean_string_list,
)
from apps.orgpages.services.guards import (
    lock_page,
    relock_actor,
    require_page_permission,
)
from apps.orgpages.services.public_cache import bust_public_page

logger = logging.getLogger(__name__)

CONTACT_BOOLS = (
    "enquiry_form_enabled", "whatsapp_enabled", "call_enabled", "email_enabled",
)
# switch → (its value field, the sentence when it is on without one)
CONTACT_NEEDS = {
    "whatsapp_enabled": ("whatsapp_number", "Add your WhatsApp number to turn this on."),
    "call_enabled": ("call_number", "Add a phone number to turn this on."),
    "email_enabled": ("contact_email", "Add an email address to turn this on."),
}


def _clean_contact(payload):
    cleaned = {}
    for field in CONTACT_BOOLS:
        if field in payload:
            cleaned[field] = clean_bool(payload[field], field)
    for field in ("whatsapp_number", "call_number"):
        if field in payload:
            cleaned[field] = clean_phone(payload[field], field)
    if "contact_email" in payload:
        cleaned["contact_email"] = clean_email(payload["contact_email"], "contact_email")
    return cleaned


def _clean_facts(payload):
    cleaned = {}

    if "founded_year" in payload:
        cleaned["founded_year"] = clean_int(
            payload["founded_year"], "founded_year", label="Founded year",
            lo=C.FOUNDED_YEAR_MIN, hi=timezone.now().year, nullable=True,
        )
    for field, label in (("age_min", "Youngest age"), ("age_max", "Oldest age")):
        if field in payload:
            cleaned[field] = clean_int(
                payload[field], field, label=label,
                lo=C.AGE_MIN, hi=C.AGE_MAX, nullable=True,
            )
    if "coaching_languages" in payload:
        cleaned["coaching_languages"] = clean_string_list(
            payload["coaching_languages"], "coaching_languages",
            label="Languages", max_items=C.MAX_COACHING_LANGUAGES,
            min_len=1, max_len=C.LANGUAGE_MAX,
        )
    if "training_days" in payload:
        cleaned["training_days"] = clean_days(
            payload["training_days"], "training_days", at_least_one=False
        )
    if "student_range" in payload:
        cleaned["student_range"] = clean_choice(
            payload["student_range"], "student_range", label="Number of students",
            choices=PageSettings.StudentRange.values, allow_blank=True,
        )
    for field in C.SOCIAL_HOSTS:
        if field in payload:
            cleaned[field] = clean_social_url(payload[field], field)

    return cleaned


def _clean_sections(payload):
    cleaned = {}
    for key, value in payload.items():
        if key not in C.SWITCHABLE_SECTIONS:
            raise invalid("This section can't be switched off.", key)
        cleaned[key] = clean_bool(value, key)
    return cleaned


def _save(page, fields):
    for field, value in fields.items():
        setattr(page, field, value)
    page.save(update_fields=[*fields, "updated_at"])


class PageSettingsService:

    @staticmethod
    def update_contact(actor, payload):
        """PATCH page/contact. A switch that is on needs its number / email."""
        organization, member = require_page_permission(actor)
        payload = payload if isinstance(payload, dict) else {}
        fields = _clean_contact(payload)

        with transaction.atomic():
            page = lock_page(organization)
            relock_actor(member)

            for switch, (value_field, message) in CONTACT_NEEDS.items():
                on = fields.get(switch, getattr(page, switch))
                value = fields.get(value_field, getattr(page, value_field))
                if on and not value:
                    raise invalid(message, value_field)

            if fields:
                _save(page, fields)
                bust_public_page(organization.id)

        return page

    @staticmethod
    def update_facts(actor, payload):
        """PATCH page/facts, partial. The youngest age can't pass the oldest."""
        organization, member = require_page_permission(actor)
        payload = payload if isinstance(payload, dict) else {}
        fields = _clean_facts(payload)

        with transaction.atomic():
            page = lock_page(organization)
            relock_actor(member)

            age_min = fields.get("age_min", page.age_min)
            age_max = fields.get("age_max", page.age_max)
            if age_min is not None and age_max is not None and age_min > age_max:
                raise invalid("The oldest age can't be below the youngest.", "age_max")

            if fields:
                _save(page, fields)
                bust_public_page(organization.id)

        return page

    @staticmethod
    def update_sections(actor, payload):
        """PATCH page/sections ``{key: bool, …}``; an unknown key is 400 invalid."""
        organization, member = require_page_permission(actor)
        payload = payload if isinstance(payload, dict) else {}
        switches = _clean_sections(payload)

        with transaction.atomic():
            page = lock_page(organization)
            relock_actor(member)

            if switches:
                # Rebuilt from the known keys only, so a stray key written by
                # hand in the admin never survives a save.
                merged = {**page.section_switches(), **switches}
                _save(page, {"sections": merged})
                bust_public_page(organization.id)

        return page
