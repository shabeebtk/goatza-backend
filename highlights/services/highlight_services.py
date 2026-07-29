"""
Write path for player highlights (HIGHLIGHTS_SPEC.md §1, §2).

Views stay thin: they pass ``request.actor`` and the raw payload straight in and
every rule is applied here — players only, at most 10 active clips each, at most
90 seconds per clip, ownership on edit/reorder/delete.

The spec sketches these as ``create_highlight(user, ...)``, but the first
argument is the resolved ``core.actor.Actor``, not a User: "players only" is not
just a role check, it also means *acting as themselves*, and only the actor
knows whether the request was made through an organization.

Failures raise DRF exceptions, so the message reaches the client unchanged:

  * PermissionDenied (403) — you may not do this at all (not a player, acting
    as an org, not your highlight)
  * NotFound (404)         — no such live highlight
  * ValidationError (400)  — everything else (cap reached, clip too long, not a
    video, not your post, malformed reorder)
"""

import uuid

from django.db import transaction
from django.db.models import F
from django.utils import timezone
from rest_framework.exceptions import (
    NotFound,
    PermissionDenied,
    ValidationError,
)

from accounts.models import User
from highlights.models import Highlight
from posts.models import Post, PostMedia
from utils.validations import is_valid_uuid


class HighlightService:

    # Product caps (§1). Enforced here, not in the serializer, so the shell and
    # any future internal caller get the same rules as the API.
    MAX_HIGHLIGHTS = 10
    MAX_DURATION_SECONDS = 90

    # =================================================================
    # GUARDS
    # =================================================================

    @staticmethod
    def require_player(actor) -> User:
        """
        The single write gate. Returns the owning user, or raises
        PermissionDenied (→ 403) when the actor may not manage highlights:
        signed out, acting as an organization, or a non-player role.

        Public because the write views call it *before* validating the body:
        someone who may not write at all should get 403, not a critique of a
        payload that was never going to be accepted. Every write here calls it
        again anyway, so the rule still lives in exactly one place.
        """
        if actor is None:
            raise PermissionDenied(
                "You must be signed in to manage highlights."
            )

        if actor.is_org:
            raise PermissionDenied(
                "Organizations do not have highlights. Switch to your "
                "personal account to manage yours."
            )

        user = actor.user if actor.is_user else None

        if user is None:
            raise PermissionDenied(
                "You must be signed in to manage highlights."
            )

        if user.role != User.Role.PLAYER:
            raise PermissionDenied(
                f"Only players can manage highlights. Your account role is "
                f"{user.get_role_display().lower()}."
            )

        return user

    @staticmethod
    def _get_owned_highlight(user, highlight_id) -> Highlight:
        """
        Load one live highlight and prove it belongs to ``user``. Deleted clips
        read as gone (404); someone else's reads as forbidden (403).
        """
        if not is_valid_uuid(highlight_id):
            raise ValidationError(f"'{highlight_id}' is not a valid highlight id.")

        highlight = Highlight.objects.filter(
            id=highlight_id,
            is_deleted=False
        ).first()

        if highlight is None:
            raise NotFound("Highlight not found.")

        if highlight.user_id != user.id:
            raise PermissionDenied(
                "You can only manage your own highlights."
            )

        return highlight

    # =================================================================
    # FIELD CLEANING
    # =================================================================

    @staticmethod
    def _clean_title(value) -> str:
        """Trim the caption and hold it to the column width."""
        title = (value or "").strip()
        max_length = Highlight._meta.get_field("title").max_length

        if len(title) > max_length:
            raise ValidationError(
                f"Title cannot be longer than {max_length} characters."
            )

        return title

    @staticmethod
    def _clean_visibility(value) -> str:
        """Reject anything outside the three documented levels (§1)."""
        if value not in Highlight.Visibility.values:
            raise ValidationError(
                "Visibility must be one of: "
                + ", ".join(Highlight.Visibility.values)
                + "."
            )

        return value

    @staticmethod
    def _clean_optional_int(value, label: str):
        """Cloudinary metadata (duration/width/height) — absent is fine, junk is not."""
        if value is None or value == "":
            return None

        try:
            number = int(value)
        except (TypeError, ValueError):
            raise ValidationError(f"{label} must be a whole number.")

        if number < 0:
            raise ValidationError(f"{label} cannot be negative.")

        return number

    # =================================================================
    # CREATE
    # =================================================================

    @staticmethod
    def _direct_fields(payload: dict) -> dict:
        """Media fields for a direct upload — straight from the Cloudinary result."""
        file_url = (payload.get("file_url") or "").strip()
        public_id = (payload.get("public_id") or "").strip()

        if not file_url:
            raise ValidationError("The uploaded video URL (file_url) is required.")

        if not public_id:
            raise ValidationError("The uploaded video id (public_id) is required.")

        max_public_id = Highlight._meta.get_field("public_id").max_length
        if len(public_id) > max_public_id:
            raise ValidationError(
                f"The uploaded video id (public_id) cannot be longer than "
                f"{max_public_id} characters."
            )

        return {
            "file_url": file_url,
            "public_id": public_id,
            "thumbnail_url": (payload.get("thumbnail_url") or "").strip(),
            "duration": HighlightService._clean_optional_int(
                payload.get("duration"), "Duration"
            ),
            "width": HighlightService._clean_optional_int(
                payload.get("width"), "Width"
            ),
            "height": HighlightService._clean_optional_int(
                payload.get("height"), "Height"
            ),
        }

    @staticmethod
    def _promote_fields(user, source_media_id) -> tuple[dict, Post]:
        """
        Media fields promoted from one of the player's own video posts.

        The values are **copied** out of PostMedia rather than referenced, so
        the clip keeps playing after the post is deleted (§1) — ``source_post``
        is attribution only and goes null with the post.
        """
        if not is_valid_uuid(source_media_id):
            raise ValidationError(
                f"'{source_media_id}' is not a valid post media id."
            )

        media = (
            PostMedia.objects
            .select_related("post")
            .filter(id=source_media_id)
            .first()
        )

        # A bad id in the body of a create is a 400, not a missing endpoint.
        if media is None:
            raise ValidationError("That post media no longer exists.")

        # Ownership first: never disclose anything about someone else's post.
        if media.post.author_user_id != user.id:
            raise ValidationError(
                "You can only add media from your own posts."
            )

        if media.post.is_deleted:
            raise ValidationError("That post has been deleted.")

        if media.media_type != PostMedia.MediaType.VIDEO:
            raise ValidationError(
                "Only videos can be added to highlights."
            )

        return (
            {
                "file_url": media.file_url,
                "public_id": media.public_id,
                "thumbnail_url": media.thumbnail_url,
                "duration": media.duration,
                "width": media.width,
                "height": media.height,
            },
            media.post,
        )

    @staticmethod
    def create_highlight(actor, *, payload: dict) -> Highlight:
        """
        Add one clip to the player's rail.

        ``payload`` picks the mode:

          * promote — ``{"source_media_id": ..., "title"?, "visibility"?}``
            copies a video off one of the player's own posts.
          * direct  — ``{"file_url", "public_id", "thumbnail_url"?, "duration"?,
            "width"?, "height"?, "title"?, "visibility"?}`` from a Cloudinary
            direct upload.

        The new clip lands last in the rail (``max(order) + 1``).
        """
        user = HighlightService.require_player(actor)

        title = HighlightService._clean_title(payload.get("title"))

        raw_visibility = payload.get("visibility")
        visibility = (
            HighlightService._clean_visibility(raw_visibility)
            if raw_visibility not in (None, "")
            else Highlight.Visibility.FOLLOWERS_AND_RECRUITERS
        )

        source_media_id = payload.get("source_media_id")

        if source_media_id:
            media_fields, source_post = HighlightService._promote_fields(
                user,
                source_media_id
            )
        else:
            media_fields = HighlightService._direct_fields(payload)
            source_post = None

        duration = media_fields.get("duration")
        if duration is not None and duration > HighlightService.MAX_DURATION_SECONDS:
            raise ValidationError(
                f"Highlights can be at most "
                f"{HighlightService.MAX_DURATION_SECONDS} seconds long. This "
                f"clip is {duration} seconds."
            )

        with transaction.atomic():
            # Serialize this player's concurrent creates. Locking only the
            # highlight rows cannot hold the cap on its own — READ COMMITTED
            # has no gap lock, and a player with zero clips has nothing to lock
            # — so the owner row is the mutex. no_key keeps it from blocking
            # unrelated inserts that merely reference the user.
            User.objects.select_for_update(no_key=True).get(pk=user.pk)

            orders = list(
                Highlight.objects
                .select_for_update()
                .filter(user=user, is_deleted=False)
                .order_by()
                .values_list("order", flat=True)
            )

            if len(orders) >= HighlightService.MAX_HIGHLIGHTS:
                raise ValidationError(
                    f"You already have {HighlightService.MAX_HIGHLIGHTS} "
                    f"highlights. Delete one to add another."
                )

            highlight = Highlight.objects.create(
                user=user,
                title=title,
                visibility=visibility,
                order=(max(orders) + 1) if orders else 0,
                source_post=source_post,
                **media_fields,
            )

        return highlight

    # =================================================================
    # UPDATE / REORDER / DELETE
    # =================================================================

    @staticmethod
    def update_highlight(actor, highlight_id, *, title=None, visibility=None) -> Highlight:
        """
        Edit a clip's caption and/or who can see it. Pass only what changes;
        passing neither is rejected rather than silently doing nothing.
        """
        user = HighlightService.require_player(actor)
        highlight = HighlightService._get_owned_highlight(user, highlight_id)

        if title is None and visibility is None:
            raise ValidationError(
                "Nothing to update. Send a title or a visibility."
            )

        # auto_now only fires for fields listed in update_fields.
        update_fields = ["updated_at"]

        if title is not None:
            highlight.title = HighlightService._clean_title(title)
            update_fields.append("title")

        if visibility is not None:
            highlight.visibility = HighlightService._clean_visibility(visibility)
            update_fields.append("visibility")

        highlight.save(update_fields=update_fields)

        return highlight

    @staticmethod
    def reorder_highlights(actor, ordered_ids: list) -> list:
        """
        Persist a drag-to-reorder, renumbering the rail 0..n-1 in one
        transaction.

        ``ordered_ids`` must be exactly the player's live highlight ids — all of
        them, once each, and nothing else. A stale or partial list is rejected
        outright instead of half-applied, so a clip can never silently fall out
        of the rail. Returns the highlights in their new order.
        """
        user = HighlightService.require_player(actor)

        if ordered_ids is None:
            raise ValidationError("ordered_ids is required.")

        if not isinstance(ordered_ids, (list, tuple)):
            raise ValidationError(
                "ordered_ids must be a list of highlight ids."
            )

        wanted = []
        for raw_id in ordered_ids:
            if not is_valid_uuid(raw_id):
                raise ValidationError(
                    f"'{raw_id}' is not a valid highlight id."
                )
            wanted.append(uuid.UUID(str(raw_id)))

        if len(set(wanted)) != len(wanted):
            raise ValidationError(
                "The same highlight cannot appear twice in the new order."
            )

        with transaction.atomic():
            highlights = {
                highlight.id: highlight
                for highlight in (
                    Highlight.objects
                    .select_for_update()
                    .filter(user=user, is_deleted=False)
                )
            }

            unknown = set(wanted) - set(highlights)
            if unknown:
                raise ValidationError(
                    "Some of those highlights are not yours, or no longer exist."
                )

            missing = set(highlights) - set(wanted)
            if missing:
                raise ValidationError(
                    f"The new order is missing {len(missing)} of your "
                    f"highlights. Send all {len(highlights)} ids."
                )

            now = timezone.now()
            ordered = []

            for position, highlight_id in enumerate(wanted):
                highlight = highlights[highlight_id]
                highlight.order = position
                # bulk_update bypasses save(), so auto_now needs a hand.
                highlight.updated_at = now
                ordered.append(highlight)

            if ordered:
                Highlight.objects.bulk_update(ordered, ["order", "updated_at"])

        return ordered

    @staticmethod
    def delete_highlight(actor, highlight_id) -> Highlight:
        """
        Soft delete one clip. The survivors keep their ``order`` values — the
        gap closes lazily on the next reorder (§2), so this stays a single write.
        """
        user = HighlightService.require_player(actor)
        highlight = HighlightService._get_owned_highlight(user, highlight_id)

        highlight.is_deleted = True
        highlight.save(update_fields=["is_deleted", "updated_at"])

        return highlight

    # =================================================================
    # VIEWS COUNTER
    # =================================================================

    @staticmethod
    def record_view(actor, highlight) -> bool:
        """
        Bump the denormalized ``views_count``. Returns True when a view was
        counted.

        The client fires this and forgets it, so it is deliberately forgiving:
        any actor may count a view (recruiters watching is the point of the
        feature), the owner's own views never count (§2), and a clip deleted
        underneath the viewer is a silent no-op rather than an error.

        The increment is a single F() UPDATE — concurrent calls add up instead
        of overwriting each other, and none of them raise. The in-memory
        ``highlight`` keeps its stale count; re-read it if you need the total.
        """
        if highlight is None:
            return False

        # Compare ids rather than going through the selector's is_owner(): this
        # runs on every clip watched, and highlight.user costs an extra query.
        if (
            actor is not None
            and actor.is_user
            and actor.user
            and actor.user.id == highlight.user_id
        ):
            return False

        updated = Highlight.objects.filter(
            pk=highlight.pk,
            is_deleted=False
        ).update(
            views_count=F("views_count") + 1
        )

        return bool(updated)
