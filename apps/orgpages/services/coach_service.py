"""
Coaches on the page: a member of the org, or somebody who isn't on Goatza.
``page.manage``.

A member coach's name and photo are their profile's, so they cannot be set
here; a non-member coach has a typed name and, optionally, a photo uploaded as
type "org_page" (replacing or removing it deletes the old object after
commit). ``is_visible`` is the per-coach switch.

A coach whose member is no longer active is hidden EVERYWHERE — the editor
included — by ``shown_coaches_q``. It does not count toward the limit, cannot
be edited, and comes back unchanged if the person rejoins.
"""

import logging

from django.db import IntegrityError, transaction
from django.db.models import Max, Q

from apps.organization.models import OrganizationMember
from apps.orgpages import constants as C
from apps.orgpages.errors import PageError, invalid, not_found
from apps.orgpages.models import Coach
from apps.orgpages.services.cleaning import (
    clean_bool,
    clean_choice,
    clean_ids,
    clean_int,
    clean_photo,
    clean_text,
)
from apps.orgpages.services.facility_service import delete_objects_later
from apps.orgpages.services.guards import (
    lock_page,
    relock_actor,
    require_page_permission,
)
from apps.orgpages.services.public_cache import bust_public_page
from utils.validations import is_valid_uuid

logger = logging.getLogger(__name__)

COACH_NOT_FOUND = "This coach wasn't found."
ALREADY_ADDED_MESSAGE = "This person is already one of your coaches."
MEMBER_PROFILE_MESSAGE = "A member's name and photo come from their profile."


def shown_coaches_q():
    """
    Non-member coaches, and member coaches whose member is still ACTIVE — one
    Q, so it always lands in a single ``filter()`` call (the relation-lookup
    rule).
    """
    return Q(member__isnull=True) | Q(member__status=OrganizationMember.Status.ACTIVE)


def org_coaches(organization):
    return Coach.objects.filter(shown_coaches_q(), organization=organization)


def get_org_coach(organization, coach_id):
    coach = (
        org_coaches(organization).filter(id=coach_id).first()
        if is_valid_uuid(str(coach_id)) else None
    )
    if coach is None:
        raise not_found(COACH_NOT_FOUND)
    return coach


def _clean_details(payload):
    cleaned = {}
    if "role" in payload:
        cleaned["role"] = clean_choice(
            payload["role"], "role", label="Role", choices=Coach.Role.values
        )
    if "title" in payload:
        cleaned["title"] = clean_text(
            payload["title"], "title", label="Title", max_len=C.COACH_TITLE_MAX
        )
    if "licence" in payload:
        cleaned["licence"] = clean_text(
            payload["licence"], "licence", label="Licence", max_len=C.COACH_LICENCE_MAX
        )
    if "years_coaching" in payload:
        cleaned["years_coaching"] = clean_int(
            payload["years_coaching"], "years_coaching", label="Years coaching",
            lo=0, hi=C.YEARS_COACHING_MAX, nullable=True,
        )
    if "is_visible" in payload:
        cleaned["is_visible"] = clean_bool(payload["is_visible"], "is_visible")
    return cleaned


def _require_title(coach):
    if coach.role == Coach.Role.OTHER and not coach.title:
        raise invalid("Add a title, like Girls coach.", "title")


def _sent(payload, field):
    value = payload.get(field)
    return value not in (None, "")


class CoachService:

    @staticmethod
    def create(actor, payload):
        """``{member_id}`` or ``{name, photo_url?, photo_public_id?}``, plus role and details."""
        organization, member = require_page_permission(actor)
        payload = payload if isinstance(payload, dict) else {}

        if not _sent(payload, "role"):
            raise invalid("Choose a role.", "role")
        details = _clean_details(payload)

        by_member = _sent(payload, "member_id")
        name = clean_text(
            payload.get("name"), "name", label="A coach's name",
            min_len=C.NAME_MIN, max_len=C.NAME_MAX,
        )
        if by_member and name:
            raise invalid("Choose a member or type a name, not both.", "name")
        if not by_member and not name:
            raise invalid("Choose a member or type the coach's name.", "name")

        photo_url = photo_public_id = ""
        if _sent(payload, "photo_url") or _sent(payload, "photo_public_id"):
            if by_member:
                raise invalid(MEMBER_PROFILE_MESSAGE, "photo_url")
            photo_url, photo_public_id = clean_photo(
                actor, organization, payload.get("photo_url"), payload.get("photo_public_id")
            )

        with transaction.atomic():
            lock_page(organization)
            relock_actor(member)

            coach_member = None
            if by_member:
                # The default manager is active-only and the org is part of the
                # lookup: a member of another org reads like a missing one.
                member_id = payload.get("member_id")
                coach_member = (
                    OrganizationMember.objects
                    .filter(id=member_id, organization=organization)
                    .first()
                    if is_valid_uuid(str(member_id)) else None
                )
                if coach_member is None:
                    raise invalid(
                        "Choose an active member of your organization.", "member_id"
                    )
                if Coach.objects.filter(
                    organization=organization, member=coach_member
                ).exists():
                    raise PageError("already_added", ALREADY_ADDED_MESSAGE, field="member_id")

            coaches = org_coaches(organization)
            if coaches.count() >= C.MAX_COACHES:
                raise PageError("coach_limit", f"You can add up to {C.MAX_COACHES} coaches.")

            coach = Coach(
                organization=organization,
                member=coach_member,
                name="" if coach_member else name,
                photo_url=photo_url,
                photo_public_id=photo_public_id,
                **details,
            )
            _require_title(coach)
            highest = Coach.objects.filter(organization=organization).aggregate(
                highest=Max("order")
            )["highest"]
            coach.order = 0 if highest is None else highest + 1

            try:
                with transaction.atomic():
                    coach.save()
            except IntegrityError:
                raise PageError("already_added", ALREADY_ADDED_MESSAGE, field="member_id")
            bust_public_page(organization.id)

        return coach

    @staticmethod
    def update(actor, coach_id, payload):
        """Partial. ``is_visible`` is how a coach is hidden without being deleted."""
        organization, member = require_page_permission(actor)
        payload = payload if isinstance(payload, dict) else {}
        details = _clean_details(payload)

        with transaction.atomic():
            lock_page(organization)
            relock_actor(member)

            coach = get_org_coach(organization, coach_id)
            previous_photo = coach.photo_public_id

            if coach.is_member:
                if _sent(payload, "name") or _sent(payload, "photo_url") or _sent(payload, "photo_public_id"):
                    raise invalid(MEMBER_PROFILE_MESSAGE, "name")
            else:
                if "name" in payload:
                    coach.name = clean_text(
                        payload["name"], "name", label="A coach's name",
                        min_len=C.NAME_MIN, max_len=C.NAME_MAX, required=True,
                    )
                if "photo_url" in payload or "photo_public_id" in payload:
                    if _sent(payload, "photo_url") or _sent(payload, "photo_public_id"):
                        coach.photo_url, coach.photo_public_id = clean_photo(
                            actor, organization,
                            payload.get("photo_url"), payload.get("photo_public_id"),
                        )
                    else:
                        coach.photo_url = coach.photo_public_id = ""

            for field, value in details.items():
                setattr(coach, field, value)
            _require_title(coach)

            coach.save()
            if previous_photo and previous_photo != coach.photo_public_id:
                delete_objects_later([previous_photo])
            bust_public_page(organization.id)

        return coach

    @staticmethod
    def delete(actor, coach_id):
        organization, member = require_page_permission(actor)

        with transaction.atomic():
            lock_page(organization)
            relock_actor(member)

            coach = get_org_coach(organization, coach_id)
            photo = coach.photo_public_id
            coach.delete()
            delete_objects_later([photo])
            bust_public_page(organization.id)

    @staticmethod
    def reorder(actor, payload):
        """``{ids}`` must be exactly the coaches the editor lists."""
        organization, member = require_page_permission(actor)

        with transaction.atomic():
            lock_page(organization)
            relock_actor(member)

            coaches = {str(coach.id): coach for coach in org_coaches(organization)}
            ids = clean_ids(payload, coaches)

            changed = []
            for index, coach_id in enumerate(ids):
                coach = coaches[coach_id]
                if coach.order != index:
                    coach.order = index
                    changed.append(coach)
            if changed:
                Coach.objects.bulk_update(changed, ["order"])
                bust_public_page(organization.id)
