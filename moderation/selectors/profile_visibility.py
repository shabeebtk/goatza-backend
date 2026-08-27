"""
What a viewer is allowed to see of a blocked party's profile (flow doc §1.5).

TWO ASYMMETRIC STATES, and the asymmetry is the whole point:

  BLOCKER LOOKS AT THE PERSON THEY BLOCKED
      They already know they blocked them, so they get the profile SHELL —
      handle, name, avatar — plus ``is_blocked_by_me: true`` so the client can
      render "You blocked this account" and an Unblock button. Everything the
      profile owns (posts, highlights, CV, careers, achievements, counts) comes
      back empty for this viewer.

  BLOCKED PERSON LOOKS AT THE BLOCKER
      404. Not a 403, not an empty profile, not a flag — the SAME 404 an
      unknown username produces, byte for byte. ``has_blocked_me`` is therefore
      never a field anybody can read: the moment it would be true, the endpoint
      stops admitting the profile exists. A distinguishable response is how
      someone confirms they were blocked and comes back on a second account.

``hide_if_blocked`` enforces the second state by raising the TARGET MODEL'S
OWN DoesNotExist. Every one of these views already handles that exception to
produce its not-found response, so the blocked 404 travels the identical code
path as a genuinely missing row — there is no second envelope to keep in sync,
and no way for the two to drift apart later.
"""

from moderation.services.block_services import BlockService
from moderation.services.block_guard import as_actor, is_blocked


def blocked_by_me(viewer, target):
    """
    Did ``viewer`` block ``target``? One direction only.

    Distinct from ``is_blocked`` (which is symmetric): this is the flag the
    profile payload carries, and it must be true ONLY for the blocker. The
    blocked party never reaches a payload at all.
    """
    if viewer is None or target is None:
        return False

    actor = as_actor(viewer)

    if hasattr(target, "email"):          # User
        return BlockService.is_blocked_between_directed(actor, target_user=target)

    return BlockService.is_blocked_between_directed(actor, target_org=target)


def has_blocked_me(viewer, target):
    """
    Did ``target`` block ``viewer``? The state that must never be serialized —
    exported only so ``hide_if_blocked`` and the conversation payload can ask.
    """
    if viewer is None or target is None:
        return False

    viewer_identity = as_actor(viewer)
    viewer_identity = (
        viewer_identity.user if viewer_identity.is_user
        else viewer_identity.organization
    )

    actor = as_actor(target)

    if hasattr(viewer_identity, "email"):
        return BlockService.is_blocked_between_directed(
            actor, target_user=viewer_identity
        )

    return BlockService.is_blocked_between_directed(
        actor, target_org=viewer_identity
    )


def hide_if_blocked(target, viewer):
    """
    Raise ``type(target).DoesNotExist`` when ``target`` has blocked ``viewer``.

    Returns ``target`` unchanged otherwise, so it reads as a pass-through at
    the call site:

        user = hide_if_blocked(User.objects.get(username=username), actor)

    Raising the model's own DoesNotExist — rather than DRF's NotFound — is
    deliberate: the surrounding view already catches it and renders its
    not-found envelope, so the blocked response is not merely similar to the
    unknown-username response, it IS that response.
    """
    if target is None:
        return target

    if has_blocked_me(viewer, target):
        raise type(target).DoesNotExist()

    return target


def profile_block_state(viewer, target):
    """
    The block fields a profile payload carries.

    Only ``is_blocked_by_me`` is ever emitted. ``has_blocked_me`` is absent by
    construction: a viewer who could see it true has already been 404'd by
    ``hide_if_blocked``, so including it would be dead weight that a future
    refactor could turn into a leak.
    """
    return {"is_blocked_by_me": blocked_by_me(viewer, target)}


def hide_owned_content(queryset, viewer, target):
    """
    Empty out a profile-owned listing (posts, highlights, CV, careers,
    achievements) when ``viewer`` has blocked ``target``.

    The symmetric ``exclude_blocked`` already removes a blocked author from
    every MIXED listing. This is for the SINGLE-OWNER listings hanging off one
    profile, where the right answer is not "filter some rows" but "this rail is
    empty for you".
    """
    if is_blocked(viewer, target):
        return queryset.none()

    return queryset


# ─────────────────────────────────────────────────────────────
# ID-ONLY VARIANTS
#
# Some profile surfaces never load the owner as a model instance — the posts
# list resolves a handle to {"type", "id"} through UsernameService and filters
# on the id. These take that pair directly rather than forcing a lookup whose
# only purpose is to satisfy a type check.
# ─────────────────────────────────────────────────────────────

def block_state_by_id(viewer, target_id, target_type):
    """
    ``(is_blocked, is_blocked_by_me)`` between ``viewer`` and an identity known
    only by id.

    ONE query, both directions, off the Block table's partial unique indexes.
    """
    from django.db.models import Q

    from core.constant import TYPE_USER
    from moderation.models import Block

    if viewer is None or target_id is None:
        return False, False

    actor = as_actor(viewer)

    me = actor.user if actor.is_user else actor.organization
    my_col = "user" if actor.is_user else "org"
    their_col = "user" if target_type == TYPE_USER else "org"

    outgoing = Q(**{
        f"blocker_{my_col}": me,
        f"blocked_{their_col}_id": target_id,
    })
    incoming = Q(**{
        f"blocker_{their_col}_id": target_id,
        f"blocked_{my_col}": me,
    })

    rows = Block.objects.filter(outgoing | incoming).values_list(
        f"blocker_{my_col}_id", flat=True
    )

    ids = list(rows)

    if not ids:
        return False, False

    # A row whose blocker column on MY side holds MY id is one I created.
    return True, any(str(i) == str(me.id) for i in ids)


def hide_if_blocked_by_id(viewer, target_id, target_type):
    """
    True when the identity has blocked ``viewer`` and the caller must render
    its not-found response. See ``hide_if_blocked`` for why it is a 404.
    """
    is_blocked, by_me = block_state_by_id(viewer, target_id, target_type)
    return is_blocked and not by_me
