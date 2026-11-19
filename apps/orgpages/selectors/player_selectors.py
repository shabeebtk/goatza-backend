"""
Players the academy developed: who qualifies, who may be featured, and the
rows the editor shows.

A player QUALIFIES with a VERIFIED CareerEntry at this org. When they have
several (two sports, two spells), the one that represents them is the current
one, else the most recent — ``qualifying_entries``.

A player MAY BE FEATURED (``can_feature``) only with an active account, no
deletion requested, a public profile, no parent's approval outstanding and no
block with the org. Featuring is opt-in: nothing here ever adds a row.
"""

from django.db.models import Q

from apps.accounts.models import User
from apps.careers.models import CareerEntry
from apps.guardians.permissions import BLOCKING_STATUSES
from apps.moderation.selectors.blocked_filters import blocked_id_sets
from apps.orgpages.models import FeaturedPlayer
from apps.sports.models import UserSportPosition

VERIFIED = CareerEntry.VerificationStatus.VERIFIED

# The candidates list is a picker, not an export.
MAX_CANDIDATES = 100


def qualifying_entries(organization, *, user_ids=None, q=None):
    """
    ``{user_id: CareerEntry}``: each player's representative VERIFIED entry at
    ``organization`` — current first, then the latest start. ONE query
    (``DISTINCT ON user_id``).
    """
    entries = CareerEntry.objects.filter(
        organization=organization, verification_status=VERIFIED
    )
    if user_ids is not None:
        entries = entries.filter(user_id__in=list(user_ids))
    if q:
        entries = entries.filter(
            Q(user__profile__name__icontains=q) | Q(user__username__icontains=q)
        )

    entries = (
        entries
        .select_related("user", "user__profile", "sport")
        .prefetch_related("positions")
        .order_by("user_id", "-is_current", "-start_date", "-created_at")
        .distinct("user_id")
    )
    return {entry.user_id: entry for entry in entries}


def is_featurable(user, blocked_user_ids=frozenset()) -> bool:
    profile = getattr(user, "profile", None)
    return bool(
        user is not None
        and user.is_active
        and user.deletion_requested_at is None
        and profile is not None
        and profile.is_public_profile
        and user.guardian_consent_status not in BLOCKING_STATUSES
        and user.id not in blocked_user_ids
    )


def entry_still_qualifies(entry, organization) -> bool:
    """A picked row's entry, re-checked: still verified, still at this org."""
    return (
        entry is not None
        and entry.verification_status == VERIFIED
        and entry.organization_id == organization.id
    )


def years_label(entry) -> str:
    """``"2019 – 2023"``, ``"2021 – now"``, or one year for a spell inside it."""
    if entry is None or entry.start_date is None:
        return ""
    start = entry.start_date.year
    if entry.is_current or entry.end_date is None:
        return f"{start} – now"
    end = entry.end_date.year
    return str(start) if end == start else f"{start} – {end}"


def primary_position_names(entries) -> dict:
    """``{(user_id, sport_id): position name}`` for the entries' players, one query."""
    user_ids = {entry.user_id for entry in entries}
    if not user_ids:
        return {}
    return {
        (link.user_id, link.sport_id): link.position.name
        for link in (
            UserSportPosition.objects
            .filter(user_id__in=user_ids, is_primary=True)
            .select_related("position")
        )
    }


def position_name(entry, primary_positions) -> str:
    """The entry's own position, else the player's primary one for that sport."""
    positions = sorted(entry.positions.all(), key=lambda position: position.name)
    if positions:
        return positions[0].name
    return primary_positions.get((entry.user_id, entry.sport_id), "")


def _user_mini(user):
    profile = getattr(user, "profile", None)
    return {
        "id": str(user.id),
        "name": getattr(profile, "name", "") or user.username or "",
        "username": user.username or "",
        "photo": getattr(profile, "profile_photo", "") or "",
    }


def _row(user, entry, primary_positions, can_feature):
    profile = getattr(user, "profile", None)
    return {
        "user": _user_mini(user),
        "years_label": years_label(entry),
        "position": position_name(entry, primary_positions) if entry else "",
        "is_public_profile": bool(getattr(profile, "is_public_profile", False)),
        "can_feature": can_feature,
    }


def editor_players(organization, actor, *, q=None) -> dict:
    """
    ``{players, candidates}`` for GET page/players.

    ``players`` is every picked row, in order — including one that would no
    longer show (marked ``can_feature: false``), so the club can see it and
    take it off. ``candidates`` is everyone with a verified entry here who is
    not picked yet, most recent first, at most MAX_CANDIDATES (``q`` narrows
    by name or username).
    """
    blocked_user_ids, _ = blocked_id_sets(actor)

    picked = list(
        FeaturedPlayer.objects
        .filter(organization=organization)
        .select_related("user", "user__profile", "career_entry")
        .prefetch_related("career_entry__positions")
        .order_by("order", "created_at")
    )
    picked_user_ids = {row.user_id for row in picked}

    candidates = [
        entry
        for user_id, entry in qualifying_entries(organization, q=q).items()
        if user_id not in picked_user_ids
    ]
    candidates.sort(
        key=lambda entry: (not entry.is_current, -entry.start_date.toordinal())
    )
    candidates = candidates[:MAX_CANDIDATES]

    primary_positions = primary_position_names(
        [row.career_entry for row in picked] + candidates
    )

    players = []
    for row in picked:
        can_feature = entry_still_qualifies(
            row.career_entry, organization
        ) and is_featurable(row.user, blocked_user_ids)
        data = _row(row.user, row.career_entry, primary_positions, can_feature)
        data.update(
            {"id": str(row.id), "highlight": row.highlight, "order": row.order}
        )
        players.append(data)

    return {
        "players": players,
        "candidates": [
            _row(
                entry.user, entry, primary_positions,
                is_featurable(entry.user, blocked_user_ids),
            )
            for entry in candidates
        ],
    }


def featurable_entry(organization, actor, user_id):
    """
    The entry that qualifies ``user_id`` for this org's page, or None when
    they are not a candidate or may not be featured.
    """
    entry = qualifying_entries(organization, user_ids=[user_id]).get(
        _as_uuid(user_id)
    )
    if entry is None:
        return None
    blocked_user_ids, _ = blocked_id_sets(actor)
    return entry if is_featurable(entry.user, blocked_user_ids) else None


def _as_uuid(value):
    """The map above is keyed by UUID objects."""
    return User._meta.pk.to_python(value)
