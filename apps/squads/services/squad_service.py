"""
The squad itself: create, edit, photo, invite link, staff, delete.

Everything here is ``squads.manage`` (owners by default). Players in a squad
are MembershipService's. Same transaction rules: the squad row's lock first,
then the acting member re-checked (``relock_actor``); a storage delete only
after commit (``enqueue`` defers itself).
"""

import logging

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from apps.organization.models import Organization, OrganizationMember
from apps.organization.permissions import OrgPermission
from apps.sports.models import Sport
from apps.squads.errors import SquadError
from apps.squads.models import Squad, SquadMember, SquadStaff, new_invite_token
from apps.squads.services.guards import (
    get_org_squad,
    relock_actor,
    require_squads_permission,
)
from apps.squads.services.membership_service import MembershipService
from apps.squads.services.public_cache import bust_public_squads
from services.storage.validators import allowed_image_extensions, validate_media
from shared.tasks import delete_keys
from utils.background_jobs import enqueue
from utils.validations import is_valid_uuid

logger = logging.getLogger(__name__)

MANAGE = OrgPermission.SQUADS_MANAGE

NAME_MIN = 2
NAME_MAX = Squad._meta.get_field("name").max_length                  # 80
SHORT_TEXT_MAX = 20                                                  # age_group, season
TITLE_MAX = SquadStaff._meta.get_field("title").max_length           # 40
PHOTO_URL_MAX = Squad._meta.get_field("photo_url").max_length        # 500
PHOTO_ID_MAX = Squad._meta.get_field("photo_public_id").max_length   # 300

NAME_TAKEN_MESSAGE = "You already have a squad with this name."
INVALID_PHOTO_MESSAGE = "This photo couldn't be used. Upload it again."

# The text fields a payload may set, with their limits and the words for them.
_TEXT_FIELDS = (
    ("age_group", SHORT_TEXT_MAX, "Age group"),
    ("season", SHORT_TEXT_MAX, "Season"),
    ("description", Squad.DESCRIPTION_MAX_LENGTH, "Description"),
)


def _invalid(message, field):
    return SquadError("invalid_field", message, field=field)


def _clean_squad_fields(payload, *, partial):
    """
    The validated subset of ``payload``. ``partial=False`` (create) requires a
    name and a sport; every other field is optional either way and only the
    keys present are touched.
    """
    cleaned = {}

    if not partial or "name" in payload:
        name = payload.get("name")
        name = name.strip() if isinstance(name, str) else ""
        if not NAME_MIN <= len(name) <= NAME_MAX:
            raise SquadError(
                "invalid_name",
                f"A squad name is {NAME_MIN} to {NAME_MAX} characters.",
                field="name",
            )
        cleaned["name"] = name

    if not partial or "sport_id" in payload:
        sport_id = payload.get("sport_id")
        sport = (
            Sport.objects.filter(id=sport_id).first()
            if is_valid_uuid(str(sport_id)) else None
        )
        if sport is None:
            raise SquadError("invalid_sport", "Choose a sport.", field="sport_id")
        cleaned["sport"] = sport

    if "level" in payload:
        level = payload.get("level") or ""
        if level not in ("", *Squad.Level.values):
            raise _invalid("Choose a valid level.", "level")
        cleaned["level"] = level

    for field, limit, label in _TEXT_FIELDS:
        if field not in payload:
            continue
        value = payload.get(field)
        if value is None:
            value = ""
        if not isinstance(value, str):
            raise _invalid(f"{label} must be text.", field)
        value = value.strip()
        if len(value) > limit:
            raise _invalid(f"{label} can be up to {limit} characters.", field)
        cleaned[field] = value

    if "is_public" in payload:
        if not isinstance(payload.get("is_public"), bool):
            raise _invalid("is_public must be true or false.", "is_public")
        cleaned["is_public"] = payload["is_public"]

    return cleaned


def _require_name_free(organization, name, exclude_id=None):
    """Case-insensitive among the org's LIVE squads, like the DB constraint."""
    taken = Squad.objects.filter(
        organization=organization, is_deleted=False, name__iexact=name
    )
    if exclude_id:
        taken = taken.exclude(id=exclude_id)
    if taken.exists():
        raise SquadError("name_taken", NAME_TAKEN_MESSAGE, field="name")


def _save_squad(squad, **kwargs):
    """
    A save whose only realistic IntegrityError is the live-name constraint: a
    rename racing a create can both pass the check above, and the constraint
    has the last word. A savepoint, so the outer transaction survives it.
    """
    try:
        with transaction.atomic():
            squad.save(**kwargs)
    except IntegrityError:
        raise SquadError("name_taken", NAME_TAKEN_MESSAGE, field="name")


def _delete_object_later(public_id):
    """The old object, deleted after commit and retried — never fails the write."""
    if public_id:
        enqueue(delete_keys, ([public_id],))


class SquadService:

    MAX_SQUADS_PER_ORG = 50
    MAX_STAFF_PER_SQUAD = 10
    DESCRIPTION_MAX = Squad.DESCRIPTION_MAX_LENGTH

    @staticmethod
    def invite_url(squad) -> str:
        """The squad's shareable link. It only lets a player ASK to join."""
        base = settings.FRONTEND_BASE_URL.rstrip("/")
        return f"{base}/squad-invite/{squad.invite_token}"

    # -----------------------------------------------------------------
    # CREATE / EDIT
    # -----------------------------------------------------------------
    @staticmethod
    def create(actor, payload):
        organization, member = require_squads_permission(actor, MANAGE)
        payload = payload if isinstance(payload, dict) else {}
        fields = _clean_squad_fields(payload, partial=False)

        with transaction.atomic():
            # The org row is the lock for the squad cap: two creates cannot
            # both count 49.
            Organization.objects.select_for_update().filter(id=organization.id).first()
            member = relock_actor(member, MANAGE)

            if (
                Squad.objects
                .filter(organization=organization, is_deleted=False)
                .count() >= SquadService.MAX_SQUADS_PER_ORG
            ):
                raise SquadError(
                    "squad_limit",
                    f"You can have up to {SquadService.MAX_SQUADS_PER_ORG} squads.",
                )

            _require_name_free(organization, fields["name"])

            squad = Squad(
                organization=organization,
                created_by_member=member,
                **fields,
            )
            _save_squad(squad)
            bust_public_squads(organization.id)

        logger.info("[SQUAD] created | squad=%s | org=%s", squad.id, organization.id)
        return squad

    @staticmethod
    def update(actor, squad_id, payload):
        organization, member = require_squads_permission(actor, MANAGE)
        payload = payload if isinstance(payload, dict) else {}
        fields = _clean_squad_fields(payload, partial=True)

        with transaction.atomic():
            squad = get_org_squad(organization, squad_id, lock=True)
            relock_actor(member, MANAGE)

            if "name" in fields:
                _require_name_free(organization, fields["name"], exclude_id=squad.id)

            if "sport" in fields and fields["sport"].id != squad.sport_id:
                now = timezone.now()
                has_players = SquadMember.objects.filter(
                    Q(status__in=[SquadMember.Status.PENDING, SquadMember.Status.ACTIVE])
                    | Q(status=SquadMember.Status.INVITED, expires_at__gt=now),
                    squad=squad,
                ).exists()
                if has_players:
                    raise SquadError(
                        "sport_locked",
                        "The sport can't change once the squad has players.",
                        field="sport_id",
                    )
                # Expired invites are dead rows; they would otherwise carry a
                # position from the old sport.
                SquadMember.objects.filter(
                    squad=squad, status=SquadMember.Status.INVITED
                ).delete()

            for field, value in fields.items():
                setattr(squad, field, value)

            if fields:
                _save_squad(squad)
                bust_public_squads(organization.id)

        return squad

    # -----------------------------------------------------------------
    # PHOTO
    # -----------------------------------------------------------------
    @staticmethod
    def set_photo(actor, squad_id, photo_url, photo_public_id):
        """
        A photo uploaded as type "squads": ours, an image, under THIS org's own
        ``organizations/<id>/squads/`` folder. The previous object is deleted
        after commit.
        """
        organization, member = require_squads_permission(actor, MANAGE)

        url = photo_url.strip() if isinstance(photo_url, str) else ""
        public_id = photo_public_id.strip() if isinstance(photo_public_id, str) else ""

        if (
            not url
            or not public_id
            or len(url) > PHOTO_URL_MAX
            or len(public_id) > PHOTO_ID_MAX
            or not public_id.startswith(f"organizations/{organization.id}/squads/")
        ):
            raise SquadError("invalid_photo", INVALID_PHOTO_MESSAGE, field="photo_url")

        try:
            validate_media(
                actor.user,
                url,
                public_id,
                org=organization,
                allowed_extensions=allowed_image_extensions(),
            )
        except ValueError:
            raise SquadError("invalid_photo", INVALID_PHOTO_MESSAGE, field="photo_url")

        with transaction.atomic():
            squad = get_org_squad(organization, squad_id, lock=True)
            relock_actor(member, MANAGE)

            previous = squad.photo_public_id
            squad.photo_url = url
            squad.photo_public_id = public_id
            squad.save(update_fields=["photo_url", "photo_public_id", "updated_at"])

            if previous != public_id:
                _delete_object_later(previous)
            bust_public_squads(organization.id)

        return squad

    @staticmethod
    def remove_photo(actor, squad_id):
        organization, member = require_squads_permission(actor, MANAGE)

        with transaction.atomic():
            squad = get_org_squad(organization, squad_id, lock=True)
            relock_actor(member, MANAGE)

            previous = squad.photo_public_id
            squad.photo_url = ""
            squad.photo_public_id = ""
            squad.save(update_fields=["photo_url", "photo_public_id", "updated_at"])
            _delete_object_later(previous)
            bust_public_squads(organization.id)

        return squad

    # -----------------------------------------------------------------
    # INVITE LINK
    # -----------------------------------------------------------------
    @staticmethod
    def set_invite_enabled(actor, squad_id, enabled):
        organization, member = require_squads_permission(actor, MANAGE)
        if not isinstance(enabled, bool):
            raise _invalid("enabled must be true or false.", "enabled")

        with transaction.atomic():
            squad = get_org_squad(organization, squad_id, lock=True)
            relock_actor(member, MANAGE)
            squad.invite_enabled = enabled
            squad.save(update_fields=["invite_enabled", "updated_at"])

        return squad

    @staticmethod
    def reset_invite_link(actor, squad_id):
        """A new token. Every copy of the old link stops working."""
        organization, member = require_squads_permission(actor, MANAGE)

        with transaction.atomic():
            squad = get_org_squad(organization, squad_id, lock=True)
            relock_actor(member, MANAGE)
            squad.invite_token = new_invite_token()
            squad.save(update_fields=["invite_token", "updated_at"])

        logger.info("[SQUAD] invite link reset | squad=%s", squad.id)
        return squad

    # -----------------------------------------------------------------
    # STAFF
    # -----------------------------------------------------------------
    @staticmethod
    def add_staff(actor, squad_id, member_id, role, title=""):
        """
        An ACTIVE member of this org credited on the squad. A head coach is just
        ``role=head_coach``; several are allowed.
        """
        organization, member = require_squads_permission(actor, MANAGE)

        if role not in SquadStaff.Role.values:
            raise _invalid("Choose a valid staff role.", "role")

        if title is None:
            title = ""
        if not isinstance(title, str):
            raise _invalid("Title must be text.", "title")
        title = title.strip()
        if len(title) > TITLE_MAX:
            raise _invalid(f"Title can be up to {TITLE_MAX} characters.", "title")

        with transaction.atomic():
            squad = get_org_squad(organization, squad_id, lock=True)
            relock_actor(member, MANAGE)

            # The default manager is active-only, and the org is part of the
            # lookup: a member of another org reads like a missing one.
            staff_member = (
                OrganizationMember.objects
                .filter(id=member_id, organization=organization)
                .first()
                if is_valid_uuid(str(member_id)) else None
            )
            if staff_member is None:
                raise SquadError(
                    "invalid_staff",
                    "Choose an active member of your organization.",
                    field="member_id",
                )

            if SquadStaff.objects.filter(squad=squad, member=staff_member).exists():
                raise SquadError(
                    "already_staff", "This person is already on the squad's staff."
                )

            count = SquadStaff.objects.filter(squad=squad).count()
            if count >= SquadService.MAX_STAFF_PER_SQUAD:
                raise SquadError(
                    "staff_limit",
                    f"A squad can have up to {SquadService.MAX_STAFF_PER_SQUAD} staff.",
                )

            try:
                with transaction.atomic():
                    staff = SquadStaff.objects.create(
                        squad=squad,
                        member=staff_member,
                        role=role,
                        title=title,
                        order=count,
                    )
            except IntegrityError:
                raise SquadError(
                    "already_staff", "This person is already on the squad's staff."
                )
            bust_public_squads(organization.id)

        return staff

    @staticmethod
    def remove_staff(actor, squad_id, staff_id):
        organization, member = require_squads_permission(actor, MANAGE)

        with transaction.atomic():
            squad = get_org_squad(organization, squad_id, lock=True)
            relock_actor(member, MANAGE)

            deleted, _ = (
                SquadStaff.objects.filter(id=staff_id, squad=squad).delete()
                if is_valid_uuid(str(staff_id)) else (0, None)
            )
            if not deleted:
                raise SquadError(
                    "not_found", "This staff member wasn't found.", status_code=404
                )
            bust_public_squads(organization.id)

    # -----------------------------------------------------------------
    # DELETE
    # -----------------------------------------------------------------
    @staticmethod
    def delete(actor, squad_id):
        """
        Soft delete, in ONE transaction. Every ACTIVE player → LEFT with their
        career stint closed (it still counts); requests and invites deleted;
        staff deleted; counters 0; the photo deleted after commit. Nobody is
        notified.
        """
        organization, member = require_squads_permission(actor, MANAGE)

        with transaction.atomic():
            squad = get_org_squad(organization, squad_id, lock=True)
            member = relock_actor(member, MANAGE)

            active_rows = list(
                SquadMember.objects
                .select_for_update()
                .filter(squad=squad, status=SquadMember.Status.ACTIVE)
            )
            for row in active_rows:
                MembershipService._end_active(
                    squad,
                    row,
                    status=SquadMember.Status.LEFT,
                    ended_by=member,
                    how="close",
                )

            SquadMember.objects.filter(
                squad=squad,
                status__in=[SquadMember.Status.PENDING, SquadMember.Status.INVITED],
            ).delete()
            SquadStaff.objects.filter(squad=squad).delete()

            previous_photo = squad.photo_public_id

            squad.is_deleted = True
            squad.deleted_at = timezone.now()
            squad.members_count = 0
            squad.pending_count = 0
            squad.photo_url = ""
            squad.photo_public_id = ""
            squad.save(
                update_fields=[
                    "is_deleted", "deleted_at", "members_count", "pending_count",
                    "photo_url", "photo_public_id", "updated_at",
                ]
            )
            _delete_object_later(previous_photo)
            bust_public_squads(organization.id)

        logger.info(
            "[SQUAD] deleted | squad=%s | org=%s | players_released=%s",
            squad.id, organization.id, len(active_rows),
        )
