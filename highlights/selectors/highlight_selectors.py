"""
Read queries for player highlights — the visibility matrix lives here.

Every clip carries its own level (HIGHLIGHTS_SPEC.md §1):

  everyone                 → any viewer
  followers_and_recruiters → recruiters, plus actors who follow the owner
  recruiters_only          → recruiters only

"Recruiter" is a property of the *viewer*, not a stored flag: an organization
actor (its membership was already verified by ``core.actor.resolve_actor``), or
a user actor whose role is SCOUT or COACH. Being a recruiter outranks following
— a recruiter sees every level, so the follow lookup is skipped for them.

The owner sees all of their own clips whatever the per-clip setting says, but
only while acting as themselves; the same person acting as one of their
organizations is just another recruiter viewing someone else's rail.
"""

from django.db.models import QuerySet

from accounts.models import User
from connections.models import Follow
from highlights.models import Highlight


# A viewer with one of these user roles counts as a recruiter (§1).
RECRUITER_ROLES = (
    User.Role.SCOUT,
    User.Role.COACH,
)


def is_recruiter(actor) -> bool:
    """
    True when ``actor`` (a ``core.actor.Actor``) reviews players rather than
    being one: any organization actor, or a scout/coach acting as themselves.
    """
    if actor is None:
        return False

    if actor.is_org:
        return bool(actor.organization)

    return bool(
        actor.is_user
        and actor.user
        and actor.user.role in RECRUITER_ROLES
    )


def is_owner(owner_user, viewer_actor) -> bool:
    """
    True only when ``viewer_actor`` is ``owner_user`` acting as themselves.
    Acting as an organization never counts as owning the rail — highlights are
    personal (§1), so that viewer falls through to the recruiter path.
    """
    if owner_user is None or viewer_actor is None:
        return False

    return bool(
        viewer_actor.is_user
        and viewer_actor.user
        and viewer_actor.user.id == owner_user.id
    )


def is_follower_of(owner_user, viewer_actor) -> bool:
    """
    True when the viewer's acting actor follows ``owner_user``. Covers both
    edges ``connections.Follow`` supports into a user — user→user and org→user
    — so acting as an organization uses that organization's follow graph, not
    the logged-in person's.

    One EXISTS query, no rows fetched.
    """
    if owner_user is None or viewer_actor is None:
        return False

    if viewer_actor.is_user:
        if viewer_actor.user is None:
            return False
        return Follow.objects.filter(
            follower_user=viewer_actor.user,
            following_user=owner_user
        ).exists()

    if viewer_actor.is_org:
        if viewer_actor.organization is None:
            return False
        return Follow.objects.filter(
            follower_org=viewer_actor.organization,
            following_user=owner_user
        ).exists()

    return False


def visible_highlights_for(owner_user, viewer_actor) -> QuerySet:
    """
    ``owner_user``'s active highlights that ``viewer_actor`` is allowed to see,
    in rail order.

    Owner → everything of theirs. Everyone else → the allowed set built per §1:
    ``everyone`` always, plus ``followers_and_recruiters`` when the viewer is a
    recruiter or follows the owner, plus ``recruiters_only`` for recruiters.

    Costs one query for the list itself; the follow lookup only runs for a
    viewer who is neither the owner nor a recruiter (both already see that
    level). Soft-deleted clips are never returned.
    """
    if owner_user is None:
        return Highlight.objects.none()

    queryset = (
        Highlight.objects
        .filter(user=owner_user, is_deleted=False)
        .order_by("order", "created_at")
    )

    if is_owner(owner_user, viewer_actor):
        return queryset

    allowed = {Highlight.Visibility.EVERYONE}

    if is_recruiter(viewer_actor):
        allowed.add(Highlight.Visibility.FOLLOWERS_AND_RECRUITERS)
        allowed.add(Highlight.Visibility.RECRUITERS_ONLY)

    elif is_follower_of(owner_user, viewer_actor):
        allowed.add(Highlight.Visibility.FOLLOWERS_AND_RECRUITERS)

    return queryset.filter(visibility__in=allowed)


def visible_highlights_count(owner_user, viewer_actor) -> int:
    """
    How many of ``owner_user``'s highlights ``viewer_actor`` may see — the
    number behind the "▶ Highlights (n)" chip on profiles and player cards.
    """
    return visible_highlights_for(owner_user, viewer_actor).count()
