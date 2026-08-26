"""
The one block guard every write path calls.

THE MESSAGE IS ALWAYS THE SAME, and it is deliberately vague:

    "This action isn't available."

Never "you are blocked", never "this user blocked you", never a different
message for the two directions. A blocked person who can tell they were blocked
knows to come back from another account; a specific error is the leak the whole
feature exists to prevent. The guard is symmetric for the same reason — it fires
whether the actor blocked the target or the target blocked the actor, and the
two are indistinguishable from the outside.

Two shapes, because two families of caller need different exception types:

  * ``require_not_blocked(actor, identity)`` raises BlockedError, a DRF
    PermissionDenied → HTTP 403. This is the default.
  * Messaging passes ``error=BlockedParticipantError`` so the failure stays a
    ``MessageError`` and the multi-target share endpoint can keep reporting it
    per recipient instead of failing the whole request.

``blocked_response()`` renders the standard envelope for the views whose broad
``except Exception`` would otherwise flatten a 403 into a 500. Those views gain
error MAPPING only — the guard itself never lives in a view, so every other
caller of the service inherits the protection.
"""

from rest_framework.exceptions import PermissionDenied

from accounts.models import User
from core.actor import Actor
from moderation.services.block_services import BlockService
from utils.errors import error_body
from utils.response import response_data

# The only wording any blocked caller ever sees. One constant so a future
# "helpful" rewording cannot happen in just one of the seven call sites.
BLOCKED_MESSAGE = "This action isn't available."


class BlockedError(PermissionDenied):
    """403. Carries the generic message and nothing about who blocked whom."""

    default_detail = BLOCKED_MESSAGE
    default_code = "blocked"


def as_actor(identity):
    """
    Wrap a bare User/Organization as a ``core.actor.Actor``.

    Half the guards start from an Actor off the request; the other half start
    from a stored row (a post's author, a conversation participant, a mention
    target) and have no Actor to hand.

    Actor-ness is a DUCK TYPE here — see BlockService._cache_key. Anything
    exposing ``.is_user``/``.is_org`` is already an actor and passes through
    untouched, whether or not it is a core.actor.Actor.
    """
    if hasattr(identity, "is_user") and hasattr(identity, "is_org"):
        return identity

    if isinstance(identity, User):
        return Actor(actor_type="user", user=identity)

    return Actor(actor_type="organization", organization=identity)


def _target_kwargs(identity):
    """``identity`` -> the target_user=/target_org= pair BlockService expects."""
    if hasattr(identity, "is_user") and hasattr(identity, "is_org"):
        identity = identity.user if identity.is_user else identity.organization

    if isinstance(identity, User):
        return {"target_user": identity}

    return {"target_org": identity}


def is_blocked(actor, identity):
    """
    Is there a block in EITHER direction between ``actor`` and ``identity``?

    ``identity`` may be a User, an Organization or another Actor. Returns False
    for a missing side rather than raising — an anonymous actor or a deleted
    author is not a block, and callers should not each write that check.
    """
    if actor is None or identity is None:
        return False

    return BlockService.is_blocked_between(
        as_actor(actor), **_target_kwargs(identity)
    )


def require_not_blocked(actor, identity, error=BlockedError):
    """
    Raise unless ``actor`` and ``identity`` are free to interact.

    ``error`` lets messaging swap in its own MessageError subclass so a blocked
    recipient lands in the share endpoint's per-target ``failed`` list instead
    of failing every other recipient with it.
    """
    if is_blocked(actor, identity):
        raise error(BLOCKED_MESSAGE)


def blocked_response():
    """
    The standard envelope for a blocked action: 403, generic message.

    For views whose ``except Exception`` would turn a raised BlockedError into
    "Something went wrong" with a 500.
    """
    return response_data(
        success=False,
        message=BLOCKED_MESSAGE,
        status_code=403,
        data=error_body(BLOCKED_MESSAGE),
    )
