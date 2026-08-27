"""
The one read-side exclusion every listing surface calls.

``exclude_blocked(queryset, actor, ...)`` drops rows authored/owned by anyone
in the actor's blocked set — both directions at once, because
``BlockService.blocked_ids`` already unions "who I blocked" with "who blocked
me". A blocked pair therefore disappears from each other's feed, explore,
search, comments, recruitment discover and follow lists without any caller
having to know which side did the blocking.

THREE PROPERTIES THE CALLERS RELY ON:

  * ZERO COST WHEN NOBODY IS BLOCKED. The overwhelmingly common case is an
    empty set, and it returns the queryset object UNCHANGED — no ``.exclude()``,
    no extra SQL, not even a cloned queryset. The one ``blocked_ids`` call
    behind it is served from cache (Stage 2, 10 min TTL), so the steady-state
    cost of this helper on a hot feed is a single cache read.

  * SAFE FOR ANONYMOUS CALLERS. ``actor`` is None on the public surfaces;
    ``blocked_ids`` returns empty sets for None, so this no-ops rather than
    needing a guard at every public call site.

  * ONE ``exclude()``, OR-ed. Django reads ``exclude(a__in=X, b__in=Y)`` as
    NOT (a IN X AND b IN Y) — which, on a dual-actor row where exactly one of
    the two columns is ever non-NULL, excludes NOTHING. The Q()-OR form below
    is what actually works, and it is the reason this lives in one function
    instead of being inlined at eight call sites.

``user_field`` / ``org_field`` default to the dual-actor Post columns since
that is the most common shape. Pass None for either side when a queryset has
only one (a User list has no org column; a Recruitment has no user column).
"""

from django.db.models import Q

from moderation.services.block_services import BlockService


def exclude_blocked(
    queryset,
    actor,
    user_field="author_user_id",
    org_field="author_org_id",
):
    """
    Remove rows belonging to identities blocked by (or blocking) ``actor``.

    Returns the SAME queryset object when the blocked set is empty, so this is
    free to call unconditionally on any listing.
    """
    blocked = BlockService.blocked_ids(actor)

    user_ids = blocked["user_ids"]
    org_ids = blocked["org_ids"]

    # FAST PATH — the normal case. No clone, no SQL change.
    if not user_ids and not org_ids:
        return queryset

    condition = Q()

    if user_field and user_ids:
        condition |= Q(**{f"{user_field}__in": user_ids})

    if org_field and org_ids:
        condition |= Q(**{f"{org_field}__in": org_ids})

    # Possible when a queryset has only a user column and only orgs are
    # blocked (or vice versa) — nothing to exclude.
    if not condition:
        return queryset

    return queryset.exclude(condition)


def blocked_id_sets(actor):
    """
    The raw ``(user_ids, org_ids)`` pair, for the few surfaces that filter a
    Python list rather than a queryset (message-target search builds its result
    set from three prioritised sources, not one query).
    """
    blocked = BlockService.blocked_ids(actor)
    return blocked["user_ids"], blocked["org_ids"]
