"""
The checks every squads path starts with: who is acting, which squad, and may
this person be in a squad at all.

Permissions come only from apps/organization/permissions.py. Nothing here (or
anywhere in squads) compares ``member.role``.
"""

from rest_framework.exceptions import PermissionDenied

from apps.accounts.models import User
from apps.guardians.permissions import BLOCKING_STATUSES
from apps.moderation.services.block_guard import is_blocked
from apps.organization.models import OrganizationMember
from apps.organization.permissions import has_org_permission, require_org_permission
from apps.squads.errors import SquadError, cannot_add, squad_not_found
from apps.squads.models import Squad
from utils.validations import is_valid_uuid

SWITCH_TO_ORG_MESSAGE = "Switch to your organization account to do this."
SWITCH_TO_PERSONAL_MESSAGE = "Switch to your personal account to do this."
ACTOR_LOST_MESSAGE = "You can no longer manage this squad."
NOT_A_PLAYER_MESSAGE = "Only player accounts can join squads."


def require_squads_permission(actor, perm):
    """``(organization, acting OrganizationMember)``, or 403."""
    if actor is None or not actor.is_org or actor.organization is None:
        raise SquadError("switch_to_org", SWITCH_TO_ORG_MESSAGE, status_code=403)

    member = require_org_permission(actor, perm)
    return actor.organization, member


def require_user_actor(actor):
    """The person acting as themselves, or 403. For the player's own paths."""
    if actor is None or not actor.is_user or actor.user is None:
        raise SquadError(
            "switch_to_personal", SWITCH_TO_PERSONAL_MESSAGE, status_code=403
        )
    return actor.user


def get_org_squad(organization, squad_id, *, lock=False):
    """
    One live squad of ``organization``, or the 404. A squad of another org
    reads exactly like a missing one.

    ``lock=True`` takes the squad row's lock — the first lock of every squads
    write, so two writers on one squad queue here and nowhere else first.
    ``of=("self",)`` keeps the lock off the joined sport and org rows.
    """
    if not is_valid_uuid(str(squad_id)):
        raise squad_not_found()

    queryset = Squad.objects.filter(
        id=squad_id, organization=organization, is_deleted=False
    ).select_related("sport", "organization")

    if lock:
        queryset = queryset.select_for_update(of=("self",))

    squad = queryset.first()
    if squad is None:
        raise squad_not_found()

    return squad


def relock_actor(member, perm):
    """
    The acting member as they are NOW, under a lock, or 403.

    An actor is as old as its request: ``require_org_permission`` read a member
    loaded before this transaction, and the request may have waited behind the
    one that removed or demoted them. Same reason as organization_member_service.
    """
    locked = (
        OrganizationMember.all_objects
        .select_for_update(of=("self",))
        .select_related("user")
        .filter(id=member.id)
        .first()
    )

    if (
        locked is None
        or locked.status != OrganizationMember.Status.ACTIVE
        or not has_org_permission(locked, perm)
    ):
        raise PermissionDenied(ACTOR_LOST_MESSAGE)

    return locked


def check_player(user, organization):
    """
    May ``user`` be in one of ``organization``'s squads?

    Only "not a player" is said out loud. Everything else — a deactivated
    account, one waiting to be deleted, a child whose parent has not approved
    (or took it back), a block in either direction — is the same generic
    refusal, so the club can never tell a block or a locked child account from
    anything else.
    """
    if (
        user is None
        or not user.is_active
        or user.deletion_requested_at is not None
    ):
        raise cannot_add()

    if user.role != User.Role.PLAYER:
        raise SquadError("not_a_player", NOT_A_PLAYER_MESSAGE)

    if (
        user.guardian_consent_status in BLOCKING_STATUSES
        or is_blocked(user, organization)
    ):
        raise cannot_add()
