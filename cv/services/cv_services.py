"""
Write path and rules for the Sports CV.

Views stay thin: they hand ``request.actor`` and the validated body straight in,
and every rule — players only, acting as themselves, what invalidates the public
cache, how a view gets counted — is applied here.

``require_player`` is a deliberate copy of
``highlights.services.highlight_services.HighlightService.require_player``
rather than an import. The two rules happen to read the same today, but they are
about different features: highlights could gain a coach-facing variant, and the
CV's refusal message names the CV. An import would couple this app to the
highlights app so that a change to one silently changed the other.

Failures raise DRF exceptions so the message reaches the client unchanged:
PermissionDenied (403) for "you may not do this at all", ValidationError (400)
for a body that was never going to be accepted.
"""

import logging

from django.db.models import F
from rest_framework.exceptions import PermissionDenied

from accounts.models import User
from cv.models import PlayerCVSettings
from utils.cache import cache_add, cache_delete
from utils.cache_keys import CacheKeys

logger = logging.getLogger(__name__)


# One counted view per IP per half hour. Long enough that a coach reading the
# page, printing it and coming back counts once; short enough that a genuine
# second visit later in the day still registers.
VIEW_COUNT_TTL = 30 * 60

# The settings a PATCH may touch. Every one of them changes what the public
# payload contains, so any of them changing invalidates the cached CV.
TOGGLE_FIELDS = (
    "is_enabled",
    "show_contact",
    "show_attributes",
    "show_career",
    "show_achievements",
    "show_highlights",
)


def client_ident(request):
    """
    The best identity an anonymous CV read has: its IP.

    ``X-Forwarded-For`` first because the app sits behind a proxy in production
    and ``REMOTE_ADDR`` there is the proxy for everybody. Spoofable, and that is
    fine — the worst a forged header buys is a view count this code was
    deliberately keeping approximate anyway.
    """
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip()

    return request.META.get("REMOTE_ADDR") or "unknown"


class CVService:

    # =================================================================
    # GUARDS
    # =================================================================

    @staticmethod
    def require_player(actor) -> User:
        """
        The single gate on the owner endpoints. Returns the owning user, or
        raises PermissionDenied (→ 403) when the actor may not manage a CV:
        signed out, acting as an organization, or a non-player role.

        A Sports CV is a player's bio-data sheet. A coach or a scout has a
        profile, not a CV, and an organization is not a person — each refusal
        says which of the three it is, because "403" alone on a settings screen
        is indistinguishable from a bug.
        """
        if actor is None:
            raise PermissionDenied(
                "You must be signed in to manage your CV."
            )

        if actor.is_org:
            raise PermissionDenied(
                "Organizations do not have a Sports CV. Switch to your "
                "personal account to manage yours."
            )

        user = actor.user if actor.is_user else None

        if user is None:
            raise PermissionDenied(
                "You must be signed in to manage your CV."
            )

        if user.role != User.Role.PLAYER:
            raise PermissionDenied(
                f"Only players have a Sports CV. Your account role is "
                f"{user.get_role_display().lower()}."
            )

        return user

    # =================================================================
    # SETTINGS
    # =================================================================

    @staticmethod
    def get_or_create_settings(actor) -> PlayerCVSettings:
        """
        The player's settings row, created with defaults if this is their first
        visit.

        Get-or-create rather than 404: a player who has never opened the screen
        has no row, and "your CV settings do not exist" is not an answer any
        settings screen can render. The defaults on the model ARE the answer.
        """
        user = CVService.require_player(actor)
        return CVService._settings_for(user)

    @staticmethod
    def _settings_for(user) -> PlayerCVSettings:
        """The row, created with model defaults on first use."""
        settings, _ = PlayerCVSettings.objects.get_or_create(user=user)

        # Prime the FK cache — the serializer reads user.username and
        # user.profile, and get_or_create does not guarantee either path
        # leaves the object it was handed attached.
        settings.user = user

        return settings

    @staticmethod
    def update_settings(actor, payload: dict) -> PlayerCVSettings:
        """
        Apply a partial update to the player's own row. Only the keys present
        in ``payload`` are written, so the client can send one toggle.

        The public CV is cached by username for a minute; any toggle that
        changes what the payload contains invalidates it here rather than
        leaving it to expire. A player who has just switched their phone number
        off must not watch it stay up for another 59 seconds.
        """
        user = CVService.require_player(actor)
        settings = CVService._settings_for(user)

        changed = []
        for field in TOGGLE_FIELDS:
            if field not in payload:
                continue
            if getattr(settings, field) == payload[field]:
                continue
            setattr(settings, field, payload[field])
            changed.append(field)

        if changed:
            # auto_now only fires for fields named in update_fields.
            settings.save(update_fields=changed + ["updated_at"])
            CVService.invalidate_public_cv(user.username)

            logger.info(
                f"[CV SETTINGS] user={user.id} changed="
                f"{{{', '.join(f'{f}={payload[f]}' for f in changed)}}}"
            )

        return settings

    @staticmethod
    def invalidate_public_cv(username) -> None:
        """
        Drop the cached anonymous rendering of one player's CV.

        Also called from the profile privacy toggle: the CV is gated on
        ``is_public_profile``, so turning that off has to clear this key too.
        """
        if username:
            cache_delete(CacheKeys.public_cv(username))

    # =================================================================
    # VIEWS COUNTER
    # =================================================================

    @staticmethod
    def record_view(settings, request) -> bool:
        """
        Bump ``views_count`` for one read of the public CV. Returns True when a
        view was actually counted.

        Called BEFORE the response cache is consulted, deliberately: a CV doing
        the rounds in a WhatsApp group is served from cache almost every time,
        and counting after the cache check would mean counting almost nothing.

        Deduplicated per IP for half an hour via an atomic ``cache.add``, so a
        refresh loop counts once. The increment itself is a single F() UPDATE —
        concurrent reads add up rather than overwriting each other.

        The in-memory ``settings`` is nudged to match rather than re-read: the
        number is a rough interest signal on a page that is itself cached for a
        minute, and it does not deserve a query of its own on every hit.
        """
        if settings is None:
            return False

        username = settings.user.username if settings.user_id else None

        if not cache_add(
            CacheKeys.cv_view_counted(username, client_ident(request)),
            1,
            VIEW_COUNT_TTL,
        ):
            return False

        updated = PlayerCVSettings.objects.filter(pk=settings.pk).update(
            views_count=F("views_count") + 1
        )

        if updated:
            settings.views_count += 1

        return bool(updated)
