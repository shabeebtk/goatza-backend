"""
Blocking — the write side.

A block is SYMMETRIC in effect but ASYMMETRIC in the row: exactly one Block
exists, owned by the blocker, and every read path unions "who I blocked" with
"who blocked me" (``blocked_ids``). That keeps unblocking a one-row delete and
keeps the blocked party from being able to tell they were blocked by inspecting
their own list.

Two rules that separate this from FollowService, and are deliberate:

  * NO NOTIFICATION, ever. Telling someone they were blocked is the failure
    mode blocking exists to prevent.
  * Blocking TEARS DOWN the follow graph in both directions, and it does so by
    calling FollowService.unfollow rather than deleting Follow rows here — the
    followers/following/connections counters are maintained in exactly one
    place and must stay that way.

Unblocking does NOT restore the follows it removed. There is no record to
restore from, and silently re-following someone you had blocked is worse than
making the user tap Follow again.
"""

import logging

from django.db import transaction
from django.db.models import Q
from rest_framework.exceptions import PermissionDenied

from accounts.models import User
from core.actor import Actor
from moderation.models import Block
from organization.models import OrganizationMember
from utils.cache import cache_delete, cache_get, cache_set
from utils.cache_keys import CacheKeys

logger = logging.getLogger(__name__)


class BlockService:

    # Blocking on behalf of a club is a public act by the club — it severs the
    # club's follow graph and hides it from an account for good. Same pair that
    # gates privacy and verification: COACH and STAFF run the day-to-day, they
    # do not decide who the organization refuses to deal with.
    BLOCK_ROLES = (
        OrganizationMember.Role.OWNER,
        OrganizationMember.Role.ADMIN,
    )

    # Short by design — see CacheKeys.blocked_ids. The explicit invalidation on
    # every write is the real mechanism; this only bounds the damage if one of
    # those deletes is ever missed.
    BLOCKED_IDS_TIMEOUT = 600  # 10 minutes

    # =================================================================
    # GUARDS
    # =================================================================

    @staticmethod
    def require_block_permission(actor):
        """
        The org-side role gate. No-op for a user actor (you always speak for
        yourself); raises PermissionDenied (-> 403) for an org actor whose
        membership role is not OWNER/ADMIN.

        ``resolve_actor`` has already proved the logged-in user is a member of
        the org they claim to act as; this only adds the role rule on top —
        same split as CareerVerificationService.require_reviewer.
        """
        if actor is None or not actor.is_org:
            return

        member = actor.organization_member

        if member is None or member.role not in BlockService.BLOCK_ROLES:
            raise PermissionDenied(
                "Only the organization's owner or an admin can block accounts"
            )

    @staticmethod
    def _validate_target(actor, target_user=None, target_org=None):
        """
        Shared pre-flight for block/unblock. Returns an error string, or None
        when the pair is usable.

        Mirrors FollowService's self-follow handling — a string outcome the
        caller turns into ``(False, message)``, not an exception: "you cannot
        block yourself" is a normal answer to a normal request, not a fault.
        """
        if actor is None:
            return "Authentication required"

        if not target_user and not target_org:
            return "Target is required"

        if actor.is_user and target_user and actor.user.id == target_user.id:
            return "Cannot block yourself"

        if actor.is_org and target_org and actor.organization.id == target_org.id:
            return "Cannot block your own organization"

        return None

    # =================================================================
    # ACTOR / IDENTITY PLUMBING
    # =================================================================

    @staticmethod
    def _actor_filters(actor, prefix):
        """
        ``{"<prefix>_user": <User>}`` or ``{"<prefix>_org": <Organization>}``
        for the acting side — the same column-picking FollowService does inline,
        named once here because block/unblock/is_blocked_between all need it.
        """
        if actor.is_user:
            return {f"{prefix}_user": actor.user}
        return {f"{prefix}_org": actor.organization}

    @staticmethod
    def _target_filters(target_user, target_org, prefix):
        """Target counterpart of ``_actor_filters``."""
        if target_user:
            return {f"{prefix}_user": target_user}
        return {f"{prefix}_org": target_org}

    @staticmethod
    def _as_actor(identity):
        """
        Wrap a bare User/Organization as a ``core.actor.Actor``.

        Needed to run FollowService.unfollow in the REVERSE direction: the
        target follows the blocker, so the target is the actor of that row.
        Building a throwaway Actor is what lets the counter logic stay in
        FollowService instead of being reimplemented here.
        """
        if isinstance(identity, User):
            return Actor(actor_type="user", user=identity)
        return Actor(actor_type="organization", organization=identity)

    @staticmethod
    def _identity_of(actor):
        """The acting side as a bare model instance."""
        return actor.user if actor.is_user else actor.organization

    # =================================================================
    # CACHE
    # =================================================================

    @staticmethod
    def _cache_key(identity):
        """
        Cache key for an ``Actor``, a ``User`` or an ``Organization``.

        Accepts all three because the two sides of a block arrive differently:
        the blocker is an Actor off the request, the blocked party is whatever
        model instance the view resolved.

        An actor is recognised by DUCK TYPE, not ``isinstance(_, Actor)``: this
        codebase passes actor-shaped objects that are not core.actor.Actor
        instances (FeedRankingService's callers, the feed tests' stub), and
        every other consumer — FollowService included — only ever reads
        ``.is_user``/``.is_org``. An isinstance check here silently fell
        through to the org branch and blew up on ``.id``.
        """
        if hasattr(identity, "is_user") and hasattr(identity, "is_org"):
            identity = BlockService._identity_of(identity)

        if isinstance(identity, User):
            return CacheKeys.blocked_ids("user", identity.id)

        return CacheKeys.blocked_ids("organization", identity.id)

    @staticmethod
    def invalidate_blocked_ids(identity):
        """
        Drop one party's cached set. Called for BOTH parties on every write —
        the set is symmetric, so a block that only busts the blocker's key
        leaves the blocked account still seeing the blocker for the full TTL.
        """
        if identity is None:
            return

        cache_delete(BlockService._cache_key(identity))

    @staticmethod
    def blocked_ids(actor):
        """
        Returns:
        {
            "user_ids": {...},
            "org_ids": {...}
        }

        The union of both directions: identities ``actor`` blocked AND
        identities that blocked ``actor``. Sets, not lists — every caller is a
        membership test inside a loop over a feed page.

        ``actor`` is None for an anonymous caller (core.actor.resolve_actor
        returns None when there is no token). Nobody has blocked an anonymous
        viewer and they have blocked nobody, so the empty answer is correct and
        every caller's ``id in blocked_ids[...]`` test degrades to False on its
        own — same contract as FollowService.get_following_ids.
        """
        if actor is None:
            return {"user_ids": set(), "org_ids": set()}

        key = BlockService._cache_key(actor)

        cached = cache_get(key)
        if cached is not None:
            return cached

        if actor.is_user:
            made = Q(blocker_user=actor.user)
            received = Q(blocked_user=actor.user)
        else:
            made = Q(blocker_org=actor.organization)
            received = Q(blocked_org=actor.organization)

        # ONE query for both directions. Each row contributes its OTHER end:
        # a row I made contributes its blocked_*, a row aimed at me contributes
        # its blocker_*. Only one of the two columns per side is non-NULL.
        rows = Block.objects.filter(made | received).values_list(
            "blocker_user_id",
            "blocker_org_id",
            "blocked_user_id",
            "blocked_org_id",
        )

        my_id = BlockService._identity_of(actor).id

        user_ids = set()
        org_ids = set()

        for blocker_user_id, blocker_org_id, blocked_user_id, blocked_org_id in rows:
            i_am_blocker = (
                blocker_user_id == my_id if actor.is_user
                else blocker_org_id == my_id
            )

            if i_am_blocker:
                other_user_id, other_org_id = blocked_user_id, blocked_org_id
            else:
                other_user_id, other_org_id = blocker_user_id, blocker_org_id

            if other_user_id:
                user_ids.add(other_user_id)
            elif other_org_id:
                org_ids.add(other_org_id)

        result = {"user_ids": user_ids, "org_ids": org_ids}

        cache_set(key, result, timeout=BlockService.BLOCKED_IDS_TIMEOUT)

        return result

    @staticmethod
    def is_blocked_between(actor, target_user=None, target_org=None):
        """
        Is there a block in EITHER direction between ``actor`` and the target?

        The write-side guard: one indexed EXISTS, deliberately uncached. A
        cached miss here would let a message or an application through after
        the block landed, and the write paths that call it touch exactly one
        target — there is nothing to amortise.
        """
        if actor is None:
            return False

        if not target_user and not target_org:
            return False

        forward = {
            **BlockService._actor_filters(actor, "blocker"),
            **BlockService._target_filters(target_user, target_org, "blocked"),
        }

        reverse = {
            **BlockService._target_filters(target_user, target_org, "blocker"),
            **BlockService._actor_filters(actor, "blocked"),
        }

        return Block.objects.filter(Q(**forward) | Q(**reverse)).exists()

    @staticmethod
    def is_blocked_between_directed(actor, target_user=None, target_org=None):
        """
        ONE DIRECTION: did ``actor`` block the target?

        The symmetric ``is_blocked_between`` answers "may these two interact",
        which is what every write guard wants. This answers "am I the one who
        blocked them", which is what the PROFILE payload needs — the blocker
        gets a shell plus an Unblock button, while the blocked party gets a 404
        and no payload at all. Collapsing the two would hand the blocked party
        a flag that tells them what happened.
        """
        if actor is None:
            return False

        if not target_user and not target_org:
            return False

        filters = {
            **BlockService._actor_filters(actor, "blocker"),
            **BlockService._target_filters(target_user, target_org, "blocked"),
        }

        return Block.objects.filter(**filters).exists()

    # =================================================================
    # WRITES
    # =================================================================

    @staticmethod
    @transaction.atomic
    def block(actor, target_user=None, target_org=None):
        """
        actor: request.actor
        target_user OR target_org required

        IDEMPOTENT — blocking someone already blocked is a success, not
        "Already blocked". The client's button is a toggle whose state can be
        stale; a 400 on the second tap would strand it in the wrong position.
        """
        error = BlockService._validate_target(actor, target_user, target_org)
        if error:
            return False, error

        # 403 before anything is written — a COACH acting for the club never
        # gets a row created and then rolled back.
        BlockService.require_block_permission(actor)

        block_data = {
            **BlockService._actor_filters(actor, "blocker"),
            **BlockService._target_filters(target_user, target_org, "blocked"),
        }

        _, created = Block.objects.get_or_create(**block_data)

        target = target_user or target_org

        if created:
            # TEAR DOWN THE FOLLOW GRAPH — both directions, through
            # FollowService so followers/following/connections counters are
            # maintained in exactly one place. Each call is a no-op returning
            # (False, "Not following") when that direction never existed.
            BlockService._unfollow_both_ways(actor, target_user, target_org)

        # Both parties, always — including the not-created path, where the row
        # already existed but a cache entry may have been rebuilt in between.
        # Cheap, and the alternative fails open.
        BlockService.invalidate_blocked_ids(actor)
        BlockService.invalidate_blocked_ids(target)

        logger.info(
            "[BLOCK] actor=%s target=%s created=%s",
            BlockService._identity_of(actor).id, target.id, created,
        )

        return True, {
            "is_blocked": True,
            "already_blocked": not created,
        }

    @staticmethod
    def _unfollow_both_ways(actor, target_user=None, target_org=None):
        """
        Remove the actor -> target and target -> actor follows, if they exist.

        The reverse direction needs the TARGET as the actor, hence ``_as_actor``.
        Neither call needs an existence check first: unfollow is already a no-op
        on a pair that never followed.

        Imported HERE, not at module scope: FollowService now imports the block
        guard (a follow is refused between a blocked pair), so a top-level
        import closes the cycle at startup.
        """
        from connections.services.follow_services import FollowService

        target = target_user or target_org

        # actor -> target
        FollowService.unfollow(
            actor=actor,
            target_user=target_user,
            target_org=target_org,
        )

        # target -> actor
        actor_identity = BlockService._identity_of(actor)

        FollowService.unfollow(
            actor=BlockService._as_actor(target),
            target_user=actor_identity if actor.is_user else None,
            target_org=actor_identity if actor.is_org else None,
        )

    @staticmethod
    @transaction.atomic
    def unblock(actor, target_user=None, target_org=None):
        """
        Remove the block. IDEMPOTENT for the same toggle-state reason as
        ``block``.

        Does NOT restore the follows the block removed — see the module
        docstring.
        """
        error = BlockService._validate_target(actor, target_user, target_org)
        if error:
            return False, error

        BlockService.require_block_permission(actor)

        filters = {
            **BlockService._actor_filters(actor, "blocker"),
            **BlockService._target_filters(target_user, target_org, "blocked"),
        }

        deleted, _ = Block.objects.filter(**filters).delete()

        target = target_user or target_org

        BlockService.invalidate_blocked_ids(actor)
        BlockService.invalidate_blocked_ids(target)

        logger.info(
            "[UNBLOCK] actor=%s target=%s deleted=%s",
            BlockService._identity_of(actor).id, target.id, bool(deleted),
        )

        return True, {
            "is_blocked": False,
            "was_blocked": bool(deleted),
        }
