"""
Programs & fees: create, edit, delete, reorder. ``page.manage``.

Create needs every field; an edit is partial and its cross-field rules (ages
in order, ends after it starts, the fee's shape) are checked on the MERGED
program. The sport must be one of the org's own sports (OrganizationSport),
and an org with none gets ``no_sport`` before anything else is looked at.
"""

import logging

from django.db import transaction
from django.db.models import Max

from apps.organization.models import OrganizationLocation, OrganizationSport
from apps.orgpages import constants as C
from apps.orgpages.errors import PageError, invalid, no_sport, not_found
from apps.orgpages.models import Program
from apps.orgpages.services.cleaning import (
    clean_bool,
    clean_choice,
    clean_days,
    clean_ids,
    clean_int,
    clean_text,
    clean_time,
)
from apps.orgpages.services.guards import (
    lock_page,
    relock_actor,
    require_page_permission,
)
from apps.orgpages.services.public_cache import bust_public_page
from utils.validations import is_valid_uuid

logger = logging.getLogger(__name__)

PROGRAM_NOT_FOUND = "This program wasn't found."

# Required on create, in the order a form shows them.
_REQUIRED = (
    "name", "sport_id", "age_min", "age_max", "level", "days",
    "start_time", "end_time", "fee_mode",
)


def _has_sport(organization):
    return OrganizationSport.objects.filter(organization=organization).exists()


def _clean_sport(organization, value):
    sport_link = (
        OrganizationSport.objects
        .filter(organization=organization, sport_id=value)
        .select_related("sport")
        .first()
        if is_valid_uuid(str(value)) else None
    )
    if sport_link is None:
        raise invalid("Choose one of your page's sports.", "sport_id")
    return sport_link.sport


def _clean_location(organization, value):
    if value in (None, ""):
        return None
    location = (
        OrganizationLocation.objects.filter(id=value, organization=organization).first()
        if is_valid_uuid(str(value)) else None
    )
    if location is None:
        raise invalid("Choose one of your locations.", "location_id")
    return location


def _clean_fields(organization, payload, *, creating):
    if creating:
        for field in _REQUIRED:
            if payload.get(field) in (None, "", []):
                if field == "days":
                    raise invalid("Choose at least one day.", field)
                raise invalid("This is required.", field)

    cleaned = {}

    if "name" in payload:
        cleaned["name"] = clean_text(
            payload["name"], "name", label="A program name",
            min_len=C.NAME_MIN, max_len=C.NAME_MAX, required=True,
        )
    if "sport_id" in payload:
        cleaned["sport"] = _clean_sport(organization, payload["sport_id"])
    for field, label in (("age_min", "Youngest age"), ("age_max", "Oldest age")):
        if field in payload:
            cleaned[field] = clean_int(
                payload[field], field, label=label, lo=C.AGE_MIN, hi=C.AGE_MAX
            )
    if "level" in payload:
        cleaned["level"] = clean_choice(
            payload["level"], "level", label="Level", choices=Program.Level.values
        )
    if "days" in payload:
        cleaned["days"] = clean_days(payload["days"], "days", at_least_one=True)
    if "start_time" in payload:
        cleaned["start_time"] = clean_time(payload["start_time"], "start_time", label="Start time")
    if "end_time" in payload:
        cleaned["end_time"] = clean_time(payload["end_time"], "end_time", label="End time")
    if "location_id" in payload:
        cleaned["location"] = _clean_location(organization, payload["location_id"])
    if "fee_mode" in payload:
        cleaned["fee_mode"] = clean_choice(
            payload["fee_mode"], "fee_mode", label="Fee option",
            choices=Program.FeeMode.values,
        )
    if "fee_amount" in payload:
        cleaned["fee_amount"] = clean_int(
            payload["fee_amount"], "fee_amount", label="The fee",
            lo=C.FEE_AMOUNT_MIN, hi=C.FEE_AMOUNT_MAX, nullable=True,
        )
    if "fee_per" in payload:
        fee_per = clean_choice(
            payload["fee_per"], "fee_per", label="Fee period",
            choices=Program.FeePer.values, allow_blank=True,
        )
        cleaned["fee_per"] = fee_per or None
    if "currency" in payload:
        currency = payload["currency"]
        currency = currency.strip().upper() if isinstance(currency, str) else ""
        if len(currency) != 3 or not currency.isalpha():
            raise invalid("Choose a valid currency.", "currency")
        cleaned["currency"] = currency
    if "free_trial" in payload:
        cleaned["free_trial"] = clean_bool(payload["free_trial"], "free_trial")
    if "spots" in payload:
        cleaned["spots"] = clean_choice(
            payload["spots"], "spots", label="Spots", choices=Program.Spots.values
        )

    return cleaned


def _apply(program, cleaned):
    """
    Merge ``cleaned`` onto ``program`` and check what spans fields. "Contact
    for fees" clears the amount and the period, as the DB check requires.
    """
    for field, value in cleaned.items():
        setattr(program, field, value)

    if program.age_min > program.age_max:
        raise invalid("The oldest age can't be below the youngest.", "age_max")
    if program.end_time <= program.start_time:
        raise invalid("The end time must be after the start time.", "end_time")

    if program.fee_mode == Program.FeeMode.AMOUNT:
        if program.fee_amount is None:
            raise invalid("Enter the fee, or choose Contact for fees.", "fee_amount")
        if not program.fee_per:
            raise invalid("Choose how often the fee is paid.", "fee_per")
    else:
        program.fee_amount = None
        program.fee_per = None


def get_org_program(organization, program_id):
    program = (
        Program.objects.filter(id=program_id, organization=organization)
        .select_related("sport", "location")
        .first()
        if is_valid_uuid(str(program_id)) else None
    )
    if program is None:
        raise not_found(PROGRAM_NOT_FOUND)
    return program


class ProgramService:

    @staticmethod
    def create(actor, payload):
        organization, member = require_page_permission(actor)
        payload = payload if isinstance(payload, dict) else {}

        if not _has_sport(organization):
            raise no_sport()
        cleaned = _clean_fields(organization, payload, creating=True)

        with transaction.atomic():
            lock_page(organization)
            relock_actor(member)

            existing = Program.objects.filter(organization=organization)
            if existing.count() >= C.MAX_PROGRAMS:
                raise PageError(
                    "program_limit", f"You can add up to {C.MAX_PROGRAMS} programs."
                )

            program = Program(organization=organization, currency="INR")
            _apply(program, cleaned)
            highest = existing.aggregate(highest=Max("order"))["highest"]
            program.order = 0 if highest is None else highest + 1
            program.save()
            bust_public_page(organization.id)

        logger.info("[PAGE] program created | program=%s | org=%s", program.id, organization.id)
        return program

    @staticmethod
    def update(actor, program_id, payload):
        organization, member = require_page_permission(actor)
        payload = payload if isinstance(payload, dict) else {}

        if "sport_id" in payload and not _has_sport(organization):
            raise no_sport()
        cleaned = _clean_fields(organization, payload, creating=False)

        with transaction.atomic():
            lock_page(organization)
            relock_actor(member)

            program = get_org_program(organization, program_id)
            if cleaned:
                _apply(program, cleaned)
                program.save()
                bust_public_page(organization.id)

        return program

    @staticmethod
    def delete(actor, program_id):
        organization, member = require_page_permission(actor)

        with transaction.atomic():
            lock_page(organization)
            relock_actor(member)

            program = get_org_program(organization, program_id)
            program.delete()
            bust_public_page(organization.id)

    @staticmethod
    def reorder(actor, payload):
        """``{ids}`` must be exactly the org's programs; each takes its index."""
        organization, member = require_page_permission(actor)

        with transaction.atomic():
            lock_page(organization)
            relock_actor(member)

            programs = {
                str(program.id): program
                for program in Program.objects.filter(organization=organization)
            }
            ids = clean_ids(payload, programs)

            changed = []
            for index, program_id in enumerate(ids):
                program = programs[program_id]
                if program.order != index:
                    program.order = index
                    changed.append(program)
            if changed:
                Program.objects.bulk_update(changed, ["order"])
                bust_public_page(organization.id)
