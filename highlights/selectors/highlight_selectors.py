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

from django.db.models import Count, Q, QuerySet

from accounts.models import User
from connections.models import Follow
from highlights.models import Highlight, HighlightView


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


def followed_user_ids_among(owner_ids, viewer_actor) -> set:
    """
    Which of ``owner_ids`` the viewer's acting actor follows — one query,
    restricted to the ids asked about. Empty for an anonymous/odd actor.
    """
    if not owner_ids or viewer_actor is None:
        return set()

    queryset = Follow.objects.filter(following_user_id__in=owner_ids)

    if viewer_actor.is_user and viewer_actor.user:
        queryset = queryset.filter(follower_user=viewer_actor.user)
    elif viewer_actor.is_org and viewer_actor.organization:
        queryset = queryset.filter(follower_org=viewer_actor.organization)
    else:
        return set()

    return set(queryset.values_list("following_user_id", flat=True))


def visible_highlight_counts_for(owner_ids, viewer_actor) -> dict:
    """
    ``{user_id: visible_count}`` for a whole PAGE of players — what the
    "▶ Highlights (n)" chip needs on applicant rows and explore player cards.

    Deliberately batched: one grouped COUNT for the page (plus at most one
    follow query), never one per row. A list endpoint must not slow down or fan
    out because a chip was added to its card (§3 performance).

    Same matrix as :func:`visible_highlights_for`. A recruiter sees every level,
    so their count needs no follow lookup at all; for anyone else the
    ``followers_and_recruiters`` level is counted only for the owners they
    actually follow. Owners are NOT special-cased — a player browsing explore
    sees their own card counted the way others see it, which is what a
    discovery surface should show.
    """
    ids = [oid for oid in set(owner_ids or []) if oid]
    if not ids:
        return {}

    visible = Q(visibility=Highlight.Visibility.EVERYONE)

    if is_recruiter(viewer_actor):
        visible |= Q(
            visibility__in=(
                Highlight.Visibility.FOLLOWERS_AND_RECRUITERS,
                Highlight.Visibility.RECRUITERS_ONLY,
            )
        )
    else:
        followed = followed_user_ids_among(ids, viewer_actor)
        if followed:
            visible |= Q(
                visibility=Highlight.Visibility.FOLLOWERS_AND_RECRUITERS,
                user_id__in=followed,
            )

    rows = (
        Highlight.objects
        .filter(visible, user_id__in=ids, is_deleted=False)
        .values("user_id")
        .annotate(total=Count("id"))
    )

    return {row["user_id"]: row["total"] for row in rows}


def highlight_stats_for(owner_user) -> dict:
    """
    The owner's analytics for their whole reel — per clip and in total.

    Two numbers, deliberately different:

      * ``views_count`` — every play, the denormalized counter on the clip.
      * ``recruiter_viewers`` — DISTINCT recruiters, from the deduplicated
        ``HighlightView`` rows. A scout who rewatches all week counts once.

    Distinct viewers are counted as ``distinct users + distinct orgs``: the two
    columns are mutually exclusive by constraint, so the sum is the true number
    of distinct actors without a UNION.

    Three queries regardless of reel size: the clips, the per-clip grouping, and
    the reel-wide distinct count (which is NOT the sum of the per-clip numbers —
    one recruiter watching three clips is one recruiter).
    """
    if owner_user is None:
        return {
            "totals": {
                "highlights": 0,
                "views": 0,
                "recruiter_viewers": 0,
            },
            "results": [],
        }

    clips = list(
        Highlight.objects
        .filter(user=owner_user, is_deleted=False)
        .order_by("order", "created_at")
    )

    clip_ids = [clip.id for clip in clips]

    per_clip = {}
    if clip_ids:
        rows = (
            HighlightView.objects
            .filter(highlight_id__in=clip_ids, is_recruiter=True)
            .values("highlight_id")
            .annotate(
                users=Count("viewer_user", distinct=True),
                orgs=Count("viewer_org", distinct=True),
            )
        )
        per_clip = {
            row["highlight_id"]: row["users"] + row["orgs"]
            for row in rows
        }

    reel_recruiters = 0
    if clip_ids:
        totals = (
            HighlightView.objects
            .filter(highlight_id__in=clip_ids, is_recruiter=True)
            .aggregate(
                users=Count("viewer_user", distinct=True),
                orgs=Count("viewer_org", distinct=True),
            )
        )
        reel_recruiters = (totals["users"] or 0) + (totals["orgs"] or 0)

    return {
        "totals": {
            "highlights": len(clips),
            "views": sum(clip.views_count for clip in clips),
            "recruiter_viewers": reel_recruiters,
        },
        "results": [
            {
                "id": clip.id,
                "title": clip.title,
                "thumbnail_url": clip.thumbnail_url,
                "views_count": clip.views_count,
                "recruiter_viewers": per_clip.get(clip.id, 0),
            }
            for clip in clips
        ],
    }
