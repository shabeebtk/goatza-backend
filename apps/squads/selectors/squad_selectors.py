"""
Read queries for the club's squads screens.

Every list is built so a page costs a constant number of queries: the rows
with their user, profile and position joined, and one more query for every
player's primary position at once.

A member whose account is deactivated or waiting to be deleted is left out of
every list here. Their row is untouched; they just are not shown.
"""

from collections import defaultdict

from django.db.models import Count, OuterRef, Prefetch, Q, Subquery
from django.utils import timezone

from apps.organization.models import OrganizationMember
from apps.sports.models import UserSportPosition
from apps.squads.models import Squad, SquadMember, SquadStaff
from apps.squads.selectors.config_selectors import squad_config_for
from utils.validations import is_valid_uuid

Status = SquadMember.Status

# The roster section for a player with no position, or a position with no group.
OTHER_GROUP = "Other"

_LIVE_USER = {
    "user__is_active": True,
    "user__deletion_requested_at__isnull": True,
}


def primary_positions_for(user_ids, sport_id) -> dict:
    """``{user_id: SportPosition}`` — each player's primary position for a sport, in one query."""
    user_ids = list(user_ids)
    if not user_ids:
        return {}

    return {
        link.user_id: link.position
        for link in (
            UserSportPosition.objects
            .filter(
                user_id__in=user_ids,
                sport_id=sport_id,
                is_primary=True,
                position__sport_id=sport_id,
            )
            .select_related("position")
        )
    }


def _open_invites_count():
    """Read-time expiry: an invite past its date is not open, whatever its status says."""
    return Count(
        "memberships",
        filter=Q(
            memberships__status=Status.INVITED,
            memberships__expires_at__gt=timezone.now(),
        ),
        distinct=True,
    )


def org_squads(organization, sport_id=None):
    """The org's live squads, oldest first, each with ``open_invites_count``."""
    squads = (
        Squad.objects
        .filter(organization=organization, is_deleted=False)
        .select_related("sport")
        .annotate(open_invites_count=_open_invites_count())
        .order_by("created_at")
    )

    if sport_id:
        if not is_valid_uuid(str(sport_id)):
            return squads.none()
        squads = squads.filter(sport_id=sport_id)

    return squads


def squad_detail(organization, squad_id):
    """
    One live squad of the org with everything SquadDetail renders, or None:
    ``open_invites_count``, ``captain_member_id``, and ``active_staff`` — the
    staff whose org membership is still ACTIVE (the relation-lookup rule: the
    status is filtered in the same ``filter()`` as the rest).
    """
    if not is_valid_uuid(str(squad_id)):
        return None

    captain = SquadMember.objects.filter(
        squad=OuterRef("pk"), status=Status.ACTIVE, is_captain=True
    ).values("id")[:1]

    staff = (
        SquadStaff.objects
        .filter(member__status=OrganizationMember.Status.ACTIVE)
        .select_related("member__user__profile")
        .order_by("order", "created_at")
    )

    return (
        Squad.objects
        .filter(id=squad_id, organization=organization, is_deleted=False)
        .select_related("sport", "sport__squad_config", "organization")
        .annotate(
            open_invites_count=_open_invites_count(),
            captain_member_id=Subquery(captain),
        )
        .prefetch_related(Prefetch("staff", queryset=staff, to_attr="active_staff"))
        .first()
    )


def display_name(user) -> str:
    profile = getattr(user, "profile", None)
    return getattr(profile, "name", "") or user.username or ""


def roster_sort_key(row):
    """Jersey number ascending (no number last), then name."""
    return (
        row.jersey_number is None,
        row.jersey_number or 0,
        display_name(row.user).casefold(),
    )


def group_members(rows, group_order) -> list:
    """
    ``[{name, members}]`` — THE roster order, shared by the club's roster and
    the showcase so the two can never disagree.

    A row's group is its position's ``group``; no position, or a blank group,
    is "Other". Groups follow the sport's ``group_order``, then any other
    groups alphabetically, then "Other". Inside a group: ``roster_sort_key``.
    """
    grouped = defaultdict(list)
    for row in rows:
        group = (row.position.group if row.position else "") or OTHER_GROUP
        grouped[group].append(row)

    def group_rank(name):
        if name in group_order:
            return (0, group_order.index(name), "")
        if name == OTHER_GROUP:
            return (2, 0, "")
        return (1, 0, name.casefold())

    return [
        {"name": name, "members": sorted(members, key=roster_sort_key)}
        for name, members in sorted(grouped.items(), key=lambda item: group_rank(item[0]))
    ]


def roster(squad) -> dict:
    """
    ``{count, groups: [{name, members}], primary_positions}`` for the ACTIVE
    players whose accounts are live, grouped by ``group_members``.
    """
    rows = list(
        SquadMember.objects
        .filter(squad=squad, status=Status.ACTIVE, **_LIVE_USER)
        .select_related("user", "user__profile", "position")
    )

    groups = group_members(
        rows, squad_config_for(squad.sport, with_positions=False)["group_order"]
    )

    return {
        "count": len(rows),
        "groups": groups,
        "primary_positions": primary_positions_for(
            {row.user_id for row in rows}, squad.sport_id
        ),
    }


def pending_requests(squad):
    """Link requests waiting for the club, oldest first."""
    return list(
        SquadMember.objects
        .filter(squad=squad, status=Status.PENDING, **_LIVE_USER)
        .select_related("user", "user__profile")
        .order_by("requested_at")
    )


def open_invites(squad):
    """Invites the players have not answered yet and that have not expired, oldest first."""
    return list(
        SquadMember.objects
        .filter(
            squad=squad,
            status=Status.INVITED,
            expires_at__gt=timezone.now(),
            **_LIVE_USER,
        )
        .select_related(
            "user",
            "user__profile",
            "position",
            "invited_by_member__user__profile",
            "recruitment_application__recruitment",
        )
        .order_by("requested_at")
    )


# Where career_entries_for puts each entry's public squad membership.
CAREER_SQUAD_ATTR = "public_squad_memberships"


def career_squad_memberships():
    """
    The memberships a career entry may name: ACTIVE, in a live, ``is_public``
    squad, earliest joined first. A hidden squad never leaks through someone's
    career.
    """
    return (
        SquadMember.objects
        .filter(
            status=Status.ACTIVE,
            squad__is_deleted=False,
            squad__is_public=True,
        )
        .select_related("squad", "position")
        .order_by("joined_at", "requested_at")
    )


def career_squad_prefetch():
    """For every selector that lists career entries: no query per row."""
    return Prefetch(
        "squad_memberships",
        queryset=career_squad_memberships(),
        to_attr=CAREER_SQUAD_ATTR,
    )


def members_for_response(member_ids) -> list:
    """Membership rows joined for MemberItem, in the order asked for."""
    member_ids = list(member_ids)
    rows = {
        row.id: row
        for row in (
            SquadMember.objects
            .select_related("squad", "user", "user__profile", "position")
            .filter(id__in=member_ids)
        )
    }
    return [rows[member_id] for member_id in member_ids if member_id in rows]
