"""
The one write path for handles.

Every place that creates or changes a username — profile edit, org create, org
edit, the auto-generators behind email signup and Google OAuth — goes through
``UsernameService``. Nothing else may write ``User.username`` or
``Organization.username``, because those columns are only unique within their
own table and the cross-table lock lives in ``UsernameRegistry``.
"""

import logging
import random
import re

from django.db import IntegrityError, transaction

from core.constant import TYPE_USER, TYPE_ORGANIZATION
from usernames.exceptions import UsernameTaken
from usernames.models import UsernameRegistry
from utils.cache import cache_get, cache_set, cache_delete_many
from utils.cache_keys import CacheKeys
from utils.validations import (
    RESERVED_USERNAMES,
    USERNAME_MAX_LENGTH,
    USERNAME_MIN_LENGTH,
    validate_username_format,
)

logger = logging.getLogger(__name__)

# What a generated handle falls back to when the source name survives
# slugification as nothing usable (emoji-only display names, punctuation, a
# blank email local-part).
GENERATE_FALLBACK = {
    TYPE_USER: "player",
    TYPE_ORGANIZATION: "club",
}

# Room reserved at the end of a generated handle for its numeric suffix — the
# WIDEST one generate() may append, so the stem is truncated once, up front,
# and no suffix can ever push the result past USERNAME_MAX_LENGTH.
GENERATE_SUFFIX_ROOM = 8

# How many times generate_and_claim() re-rolls when a concurrent signup
# takes the handle between the read and the insert.
GENERATE_CLAIM_ATTEMPTS = 5

# How long a resolved handle stays cached. The same 300s the old two-query
# lookup used — the difference is that claim()/release() now delete it.
RESOLVE_CACHE_TTL = 300


def _owner_field(owner_type: str) -> str:
    """Actor type -> the registry column that holds it."""
    return "user" if owner_type == TYPE_USER else "organization"


class UsernameService:

    # ── internals ─────────────────────────────────────────────

    @staticmethod
    def _owner(user, organization):
        """Enforce the dual-actor 'exactly one' rule at the service boundary."""
        if bool(user) == bool(organization):
            raise ValueError("Pass exactly one of user or organization")
        return (TYPE_USER, user) if user else (TYPE_ORGANIZATION, organization)

    @staticmethod
    def _cache_keys_for(username: str, owner_type: str) -> list:
        """
        Every cache entry keyed by this handle.

        ``cv:viewed:<username>:<ident>`` is deliberately absent: it is a
        per-viewer "already counted" latch with a short TTL, not a content
        cache, and its idents cannot be enumerated. Losing those on a rename
        would at worst let one extra view be counted.
        """
        if not username:
            return []

        keys = [CacheKeys.profile_lookup(username)]

        if owner_type == TYPE_USER:
            keys.append(CacheKeys.public_user_profile(username))
            keys.append(CacheKeys.public_cv(username))
            # Unused today, but it is keyed by handle — if it comes back it
            # must not come back stale.
            keys.append(CacheKeys.user_details(username, "mini"))
            keys.append(CacheKeys.user_details(username, "full"))
        else:
            keys.append(CacheKeys.public_org_profile(username))

        return keys

    @classmethod
    def _invalidate(cls, owner_type: str, *usernames: str) -> None:
        """
        Delete TWICE: once now, once after the enclosing transaction commits.

        The immediate delete is what the caller (and the tests) can rely on.
        The on_commit one closes the window where a concurrent reader
        repopulates the key from the pre-commit state between the two — which
        would leave the stale mapping in place for the full TTL, i.e. exactly
        the bug this replaces. Deleting an absent key is free, so the second
        pass costs nothing when nothing raced.
        """
        keys = []
        for username in usernames:
            keys.extend(cls._cache_keys_for(username, owner_type))
        if not keys:
            return

        cache_delete_many(keys)
        transaction.on_commit(lambda: cache_delete_many(keys))

    # ── public API ────────────────────────────────────────────

    @classmethod
    def claim(cls, username: str, *, user=None, organization=None) -> str:
        """
        Register ``username`` to this actor and write the display column.

        The unique constraint on UsernameRegistry is the arbiter — we do NOT
        pre-check availability and then insert, because that gap is exactly the
        race two simultaneous claims of the same handle would win. An
        IntegrityError from the insert IS the "taken" answer.

        Returns the normalized handle. Raises ValueError (bad format) or
        UsernameTaken.
        """
        owner_type, owner = cls._owner(user, organization)
        username = validate_username_format(username)
        field = _owner_field(owner_type)

        old_username = (owner.username or "").strip().lower()

        with transaction.atomic():
            existing = UsernameRegistry.objects.filter(**{field: owner}).first()

            # Re-claiming your own handle is a no-op, not a collision. It still
            # falls through to the display-column write below, so a row whose
            # display value drifted from the registry gets repaired.
            if existing is None or existing.username_lower != username:
                if existing is not None:
                    existing.delete()

                try:
                    # Savepoint: an IntegrityError leaves the enclosing
                    # transaction unusable, and create_organization calls this
                    # from inside its own atomic block.
                    with transaction.atomic():
                        UsernameRegistry.objects.create(
                            username_lower=username, **{field: owner}
                        )
                except IntegrityError:
                    logger.info(
                        "[USERNAME] claim rejected (taken) "
                        f"handle={username} owner_type={owner_type}"
                    )
                    raise UsernameTaken(username)

            if owner.username != username:
                owner.username = username
                owner.save(update_fields=["username", "updated_at"])

        cls._invalidate(owner_type, old_username, username)

        logger.info(
            f"[USERNAME] claimed handle={username} owner_type={owner_type} "
            f"owner={owner.id} previous={old_username or '-'}"
        )
        return username

    @classmethod
    def is_available(cls, username, *, exclude_user=None, exclude_org=None) -> bool:
        """
        Is this handle free for the given actor to take?

        Format is validated FIRST, and a malformed handle raises ValueError
        rather than returning False — "not allowed" and "somebody has it" are
        different answers, and the API has to be able to say which.
        """
        username = validate_username_format(username)

        qs = UsernameRegistry.objects.filter(username_lower=username)

        if exclude_user is not None:
            qs = qs.exclude(user=exclude_user)
        if exclude_org is not None:
            qs = qs.exclude(organization=exclude_org)

        return not qs.exists()

    @classmethod
    def generate(cls, base: str, *, owner_type: str) -> str:
        """
        A free, VALID handle derived from ``base`` (a display name, or an email
        local-part).

        Loops against UsernameRegistry, not against one table — generating
        against ``Organization`` alone is how an org used to be handed a handle
        a user already held.
        """
        if owner_type not in GENERATE_FALLBACK:
            raise ValueError(f"Unknown owner_type: {owner_type}")

        # Slugify to the charset: everything outside [a-z0-9_] goes, and the
        # doubled / leading / trailing underscore rules are applied here rather
        # than left for the validator to reject at the very end.
        slug = re.sub(r"[^a-z0-9_]", "", (base or "").lower())
        slug = re.sub(r"_+", "_", slug).strip("_")

        # ONE truncation, wide enough for the widest suffix. Doing it here
        # rather than per-attempt is what makes the stem's validity a property
        # of the stem: a truncation inside the loop could cut a mixed slug back
        # to digits, and "1234567890123456789012" + 8 digits is a numeric
        # handle the validator would reject at the very last step.
        stem = slug[: USERNAME_MAX_LENGTH - GENERATE_SUFFIX_ROOM].rstrip("_")

        # Too short, reserved, or all digits — none of those carry a suffix
        # into a valid handle, so start from the neutral base instead.
        if (
            len(stem) < USERNAME_MIN_LENGTH
            or stem.isdigit()
            or stem in RESERVED_USERNAMES
        ):
            stem = GENERATE_FALLBACK[owner_type]

        candidate = f"{stem}{random.randint(10, 99)}"
        attempts = 0

        while UsernameRegistry.objects.filter(username_lower=candidate).exists():
            attempts += 1
            # Widen the suffix as the space fills up, rather than spinning on a
            # two-digit range that may already be exhausted.
            digits = 4 if attempts < 20 else GENERATE_SUFFIX_ROOM
            candidate = (
                f"{stem}{random.randint(10 ** (digits - 1), 10 ** digits - 1)}"
            )

        # The generators are the one path that never passes through a
        # serializer, so the invariant is asserted here instead of trusted.
        assert validate_username_format(candidate) == candidate, (
            f"generate() produced an invalid handle: {candidate!r}"
        )
        return candidate

    @classmethod
    def generate_and_claim(cls, base: str, *, user=None, organization=None) -> str:
        """
        generate() + claim() for the paths that never see a serializer: email
        signup, Google OAuth, org create.

        Retries because generate() reads the registry and claim() writes it,
        and between those two a concurrent signup can take the handle. The
        unique constraint stays the arbiter — this just picks another number
        and goes again rather than failing a signup over a coin-flip.
        """
        owner_type, _ = cls._owner(user, organization)

        for _ in range(GENERATE_CLAIM_ATTEMPTS):
            candidate = cls.generate(base, owner_type=owner_type)
            try:
                return cls.claim(candidate, user=user, organization=organization)
            except UsernameTaken:
                continue

        raise UsernameTaken(
            base, "Could not allocate a username, please try again"
        )

    @classmethod
    def resolve(cls, username) -> dict:
        """
        Handle -> {"type": "user" | "organization", "id": UUID}.

        ONE indexed query on the registry, where the old implementation did a
        User lookup with an Organization fallback — the fallback being what
        made a colliding org permanently unreachable.

        Raises ValueError when nothing holds the handle.
        """
        if not username:
            raise ValueError("Username is required")

        username = str(username).strip().lower()

        cache_key = CacheKeys.profile_lookup(username)
        cached = cache_get(cache_key)
        if cached:
            return cached

        row = (
            UsernameRegistry.objects
            .filter(username_lower=username)
            .select_related("user", "organization")
            .only("username_lower", "user__id", "organization__id")
            .first()
        )

        if row is None:
            raise ValueError("Profile not found")

        result = (
            {"type": TYPE_USER, "id": row.user_id}
            if row.user_id
            else {"type": TYPE_ORGANIZATION, "id": row.organization_id}
        )

        cache_set(cache_key, result, timeout=RESOLVE_CACHE_TTL)
        return result

    @classmethod
    def release(cls, *, user=None, organization=None) -> None:
        """
        Give the handle back to the namespace. Call on account/org deletion.

        The registry row would also go by CASCADE, but going through here is
        what invalidates the cache — a deleted account whose handle keeps
        resolving is the same bug as a renamed one.
        """
        owner_type, owner = cls._owner(user, organization)
        username = (owner.username or "").strip().lower()

        UsernameRegistry.objects.filter(
            **{_owner_field(owner_type): owner}
        ).delete()

        cls._invalidate(owner_type, username)
        logger.info(
            f"[USERNAME] released handle={username or '-'} owner={owner.id}"
        )
