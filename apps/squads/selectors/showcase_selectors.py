"""
What everyone else sees of a club's squads — THE visibility rules, in one place
for both surfaces: the signed-in showcase (``/squads/showcase/…``) and the
anonymous one (``/public/organization/<username>/squads…``).

A SQUAD IS SHOWN when it is live and ``is_public``, its org is usable, and at
least one of its players is visible to this viewer. Anything else — an unknown
username, a hidden or empty squad, a suspended org, a block — is the same miss,
and the views answer every miss with one 404.

THE SHOWCASE ALWAYS APPLIES THESE RULES, including to the club's own members:
the club previews exactly what others see. Hidden squads are reachable only
through the club endpoints.

THE VIEWER:
  * Signed in (``actor``): the org must be active and not suspended, with no
    block between the viewer and the org. Players in a block with the viewer
    are dropped from everything, counts included — a count that did not match
    the list would tell them a block exists.
  * Anonymous (``actor=None``): the org must pass the public-profile predicate
    (its own setting). Players outside ``public_users_queryset()`` are not
    listed; they only count toward their group's ``hidden_count``.

A player is an ACTIVE row whose account is active with no deletion requested.
Staff follow the same rules as players (``visible_staff``): ACTIVE org members
with live accounts, nobody in a block with a signed-in viewer, and for an
anonymous viewer only public profiles — the rest are left out entirely, not
even counted.

ONE PLAYER COUNT: a squad's count is every player who counts for this viewer
(private profiles included), on the list card and on the squad page alike. A
squad still needs at least one LISTED player to be shown.

THE SITEMAP (``public_squad_sitemap_rows``) is the anonymous rule again, for
every club in one query; it shares the same three building blocks, so a squad
is crawlable exactly when the anonymous showcase would show it.

THREE QUERIES for every squad of the club at once — squads, players, staff —
after the org lookup. The list and the squad page both build on
``visible_squads``, so a squad page's "other squads" cost nothing extra.
"""

from collections import defaultdict

from django.db.models import Exists, F, OuterRef

from apps.moderation.selectors.blocked_filters import blocked_id_sets
from apps.moderation.services.block_guard import is_blocked
from apps.organization.models import Organization, OrganizationMember
from apps.squads.models import Squad, SquadMember, SquadStaff
from apps.squads.selectors.squad_selectors import roster_sort_key
from core.selectors.public_profile_selectors import (
    public_organizations_queryset,
    public_users_queryset,
)
from utils.validations import is_valid_uuid

Status = SquadMember.Status


def showcase_organization(username, actor):
    """The org whose squads this viewer may see, or None."""
    if not username:
        return None

    if actor is None:
        # The public-profile predicate itself, without the bundle's prefetches.
        return (
            public_organizations_queryset()
            .select_related("profile")
            .filter(username=username)
            .first()
        )

    organization = (
        Organization.objects
        .filter(username=username, is_active=True, is_suspended=False)
        .select_related("profile")
        .first()
    )
    if organization is None or is_blocked(actor, organization):
        return None

    return organization


def _counting_rows():
    """A player who counts: an ACTIVE row whose account is live."""
    return SquadMember.objects.filter(
        status=Status.ACTIVE,
        user__is_active=True,
        user__deletion_requested_at__isnull=True,
    )


def _is_listed_anonymously():
    """Annotation / filter for a row whose player an anonymous viewer may see by name."""
    return Exists(public_users_queryset().filter(id=OuterRef("user_id")))


def _shown_squads():
    """Live squads the club has put on its profile."""
    return Squad.objects.filter(is_deleted=False, is_public=True)


def visible_squads(organization, actor) -> list:
    """
    ``[{squad, players, shown, staff}]`` for every squad this viewer may see,
    oldest first.

    ``players`` is every player who counts (for an anonymous viewer, private
    profiles too); ``shown`` is the ones listed by name. Each row carries
    ``is_listed``. A squad with nobody shown is left out.
    """
    squads = list(
        _shown_squads()
        .filter(organization=organization)
        .select_related("sport", "sport__squad_config")
        .order_by("created_at")
    )
    if not squads:
        return []

    squad_ids = [squad.id for squad in squads]

    rows = (
        _counting_rows()
        .filter(squad_id__in=squad_ids)
        .select_related("user", "user__profile", "position")
    )

    blocked_user_ids = set()
    if actor is None:
        rows = rows.annotate(is_listed=_is_listed_anonymously())
    else:
        blocked_user_ids, _ = blocked_id_sets(actor)
        if blocked_user_ids:
            rows = rows.exclude(user_id__in=blocked_user_ids)

    players_by_squad = defaultdict(list)
    for row in rows:
        if actor is not None:
            row.is_listed = True
        players_by_squad[row.squad_id].append(row)

    staff_by_squad = defaultdict(list)
    for staff in visible_staff(actor, blocked_user_ids).filter(squad_id__in=squad_ids):
        staff_by_squad[staff.squad_id].append(staff)

    result = []
    for squad in squads:
        players = players_by_squad.get(squad.id, [])
        shown = [row for row in players if row.is_listed]
        if not shown:
            continue
        result.append(
            {
                "squad": squad,
                "players": players,
                "shown": sorted(shown, key=preview_sort_key),
                "staff": staff_by_squad.get(squad.id, []),
            }
        )

    return result


#: The sitemap feed's cap, the same as its users and organizations lists.
SITEMAP_MAX_SQUADS = 5000


def public_squad_sitemap_rows():
    """
    ``[{"org_username", "squad_id", "updated_at"}]``: every squad the ANONYMOUS
    showcase would show, freshest first, capped at ``SITEMAP_MAX_SQUADS``.

    The same rules as ``visible_squads(org, None)`` — live and ``is_public``,
    the org passes the public-profile predicate, at least one LISTED player —
    in ONE query for every club at once: the org rule is a subquery and the
    listed player an ``Exists``, so nothing runs per org or per squad.
    """
    listed_player = (
        _counting_rows()
        .filter(squad_id=OuterRef("pk"))
        .filter(_is_listed_anonymously())
    )
    return list(
        _shown_squads()
        .filter(organization__in=public_organizations_queryset())
        .filter(Exists(listed_player))
        .order_by("-updated_at")
        .values("id", "updated_at", org_username=F("organization__username"))
        [:SITEMAP_MAX_SQUADS]
    )


def visible_staff(actor, blocked_user_ids=None):
    """
    The squad staff this viewer may see, in staff order — the same privacy
    rules as players, so a person hidden as a player is never shown as staff.

    Always: an ACTIVE org member whose account is active with no deletion
    requested (all in ONE ``filter()``: the relation-lookup rule).
    Anonymous: only people in ``public_users_queryset()``. Everyone else is left
    out completely — not listed, not counted. Signed in: nobody in a block with
    the viewer (``blocked_user_ids`` from ``blocked_id_sets``; looked up here
    when not passed).
    """
    staff = (
        SquadStaff.objects
        .filter(
            member__status=OrganizationMember.Status.ACTIVE,
            member__user__is_active=True,
            member__user__deletion_requested_at__isnull=True,
        )
        .select_related("member__user__profile")
        .order_by("order", "created_at")
    )

    if actor is None:
        return staff.filter(
            Exists(public_users_queryset().filter(id=OuterRef("member__user_id")))
        )

    if blocked_user_ids is None:
        blocked_user_ids, _ = blocked_id_sets(actor)
    if blocked_user_ids:
        staff = staff.exclude(member__user_id__in=blocked_user_ids)

    return staff


def preview_sort_key(row):
    """Captain first, then the roster order."""
    return (not row.is_captain, *roster_sort_key(row))


def showcase_squad(organization, squad_id, actor):
    """
    ``(entry, others)`` for one squad page, or ``(None, [])`` for a miss.
    ``entry`` is the ``visible_squads`` dict for this squad; ``others`` the
    rest, in the same order.
    """
    if not is_valid_uuid(str(squad_id)):
        return None, []

    squads = visible_squads(organization, actor)
    entry = next(
        (item for item in squads if str(item["squad"].id) == str(squad_id)), None
    )
    if entry is None:
        return None, []

    return entry, [item for item in squads if item is not entry]
