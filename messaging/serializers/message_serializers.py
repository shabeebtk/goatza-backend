from rest_framework import serializers

from messaging.models import Message
from messaging.selectors.share_selectors import (
    ShareViewer,
    is_org_profile_shareable,
    is_post_shareable,
    is_recruitment_shareable,
    is_user_profile_shareable,
)
from shared.serializers.actor_serializers import ActorMiniSerializer

# How much of a shared post's body the preview carries.
POST_SNIPPET_LENGTH = 120

# A share whose object is gone, or which the reader may not see, renders as
# this and nothing else — no id, no author, no text.
UNAVAILABLE = {"unavailable": True}


class MessageSerializer(serializers.ModelSerializer):
    sender = serializers.SerializerMethodField()
    shared_post_preview = serializers.SerializerMethodField()
    shared_recruitment_preview = serializers.SerializerMethodField()
    shared_user_profile_preview = serializers.SerializerMethodField()
    shared_org_profile_preview = serializers.SerializerMethodField()
    is_read = serializers.SerializerMethodField()

    class Meta:
        model = Message
        fields = [
            "id",
            "content",
            "message_type",
            "created_at",
            "sender",
            "is_read",

            # media
            "media_url",
            "media_public_id",
            "media_thumbnail_url",
            "media_width",
            "media_height",
            "media_duration_ms",
            "media_size_bytes",

            # shared content
            "shared_post_preview",
            "shared_recruitment_preview",
            "shared_user_profile_preview",
            "shared_org_profile_preview",
        ]

    # ----------------------------------------
    # VIEWER
    # ----------------------------------------
    @property
    def _viewer(self) -> ShareViewer:
        """
        Who is reading. Resolved once per serializer instance — with many=True
        the same child instance renders every row, so the viewer's follow graph
        is fetched once for the whole page, not once per message.

        Sources, in order:
          * context["viewer"] — an explicit ShareViewer, or None for "no
            viewer" (the websocket fan-out passes this; see
            MessageService._trigger_realtime).
          * context["request"].actor — the normal REST path.
          * nothing → anonymous viewer, which sees public content only.
        """
        if not hasattr(self, "_viewer_cache"):
            if "viewer" in self.context:
                viewer = self.context["viewer"]
                self._viewer_cache = (
                    viewer if isinstance(viewer, ShareViewer) else ShareViewer()
                )
            else:
                request = self.context.get("request")
                self._viewer_cache = ShareViewer.from_actor(
                    getattr(request, "actor", None)
                )

        return self._viewer_cache

    def get_sender(self, obj):
        if obj.sender_user:
            return ActorMiniSerializer(obj.sender_user).data

        if obj.sender_org:
            return ActorMiniSerializer(obj.sender_org).data

        return None

    # ----------------------------------------
    # READ RECEIPT
    # ----------------------------------------
    def get_is_read(self, obj):
        """
        Has the OTHER side seen this message?

        Derived from the recipient participant's ``last_read_at`` rather than a
        column on Message: marking a thread read stays one UPDATE on one
        participant row instead of an UPDATE across every message in it, and
        that timestamp is already the source of truth behind ``unread_count``.

        Only meaningful for messages the viewer SENT — the client paints read
        ticks on its own bubbles only. Callers that don't put
        ``other_last_read_at`` in the context (the websocket fan-out, the
        conversation list) get False, which is the right default: a message
        being broadcast right now has not been read by anyone yet.
        """
        last_read = self.context.get("other_last_read_at")

        if not last_read:
            return False

        return obj.created_at <= last_read

    # ----------------------------------------
    # SHARED POST PREVIEW
    # ----------------------------------------
    def get_shared_post_preview(self, obj):
        if obj.message_type != Message.Type.SHARED_POST:
            return None

        post = obj.shared_post

        # post is None once the author deleted it (FK is SET_NULL) — never a
        # 500, always an unavailable card.
        if not is_post_shareable(post, self._viewer):
            return UNAVAILABLE

        media = list(post.media.all())   # prefetched + ordered by MessageSelector
        first_media = media[0] if media else None

        text = post.content or ""

        return {
            "unavailable": False,
            "id": str(post.id),
            "author": ActorMiniSerializer(
                post.author_user or post.author_org
            ).data,
            "text": text[:POST_SNIPPET_LENGTH],
            "is_text_truncated": len(text) > POST_SNIPPET_LENGTH,
            # Images carry no separate thumbnail — fall back to the file itself.
            "thumbnail_url": (
                (first_media.thumbnail_url or first_media.file_url)
                if first_media else ""
            ),
            "media_count": len(media),
        }

    # ----------------------------------------
    # SHARED RECRUITMENT PREVIEW
    # ----------------------------------------
    def get_shared_recruitment_preview(self, obj):
        if obj.message_type != Message.Type.SHARED_RECRUITMENT:
            return None

        recruitment = obj.shared_recruitment

        if not is_recruitment_shareable(recruitment, self._viewer):
            return UNAVAILABLE

        media = list(recruitment.media.all())   # prefetched + ordered
        cover = media[0] if media else None

        return {
            "unavailable": False,
            "id": str(recruitment.id),
            "title": recruitment.title,
            "org": ActorMiniSerializer(recruitment.organization).data,
            "sport": recruitment.sport.name if recruitment.sport else "",
            "type": recruitment.recruitment_type,
            "status": recruitment.status,
            # isoformat, not the raw datetime: this dict is built by hand rather
            # than by a DRF field, and the same payload is msgpack'd onto the
            # channel layer, which can only carry primitives.
            "deadline": (
                recruitment.application_deadline.isoformat()
                if recruitment.application_deadline else None
            ),
            "cover_url": (
                (cover.thumbnail_url or cover.file_url) if cover else ""
            ),
        }

    # ----------------------------------------
    # SHARED PROFILE PREVIEWS
    # ----------------------------------------
    # Every value below is a str/int/None. These dicts are built by hand rather
    # than by DRF fields, and the same payload is msgpack'd onto the channel
    # layer by MessageService._trigger_realtime, which can only carry
    # primitives — UserProfile.weight_kg is a DecimalField, which is why nothing
    # reads it here and why any future physical stat has to be cast.

    def get_shared_user_profile_preview(self, obj):
        if obj.message_type != Message.Type.SHARED_USER_PROFILE:
            return None

        user = obj.shared_profile_user

        # user is None once the account was hard-deleted (FK is SET_NULL), and
        # is_user_profile_shareable also rejects a deactivated or usernameless
        # one — never a 500, always an unavailable card.
        if not is_user_profile_shareable(user, self._viewer):
            return UNAVAILABLE

        profile = getattr(user, "profile", None)

        # Read off the prefetched lists (MessageSelector joins them) rather than
        # re-filtering in SQL, so a page of shared profiles stays flat.
        primary_sport = next(
            (s for s in user.sports.all() if s.is_primary), None
        )
        primary_position = next(
            (
                p for p in user.positions.all()
                if p.is_primary
                and primary_sport is not None
                and p.sport_id == primary_sport.sport_id
            ),
            None,
        )

        return {
            "unavailable": False,
            "id": str(user.id),
            "username": user.username or "",
            "name": profile.name if profile else "",
            "avatar": profile.profile_photo if profile else "",
            "headline": profile.headline if profile else "",
            "role": user.role,
            "primary_sport": primary_sport.sport.name if primary_sport else "",
            "primary_position": (
                primary_position.position.name if primary_position else ""
            ),
            # City-level only, matching the public profile payload — a chat card
            # is not a reason to carry coordinates the profile itself withholds.
            "city": profile.city if profile else "",
            "country_code": profile.country_code if profile else "",
            "followers_count": profile.followers_count if profile else 0,
        }

    def get_shared_org_profile_preview(self, obj):
        if obj.message_type != Message.Type.SHARED_ORG_PROFILE:
            return None

        org = obj.shared_profile_org

        if not is_org_profile_shareable(org, self._viewer):
            return UNAVAILABLE

        profile = getattr(org, "profile", None)

        # The card prints one city. An org's locations are business addresses
        # and there may be several — the primary one is the club's home ground,
        # which is the one that means anything at a glance.
        locations = list(org.locations.all())
        primary_location = next(
            (loc for loc in locations if loc.is_primary),
            locations[0] if locations else None,
        )

        return {
            "unavailable": False,
            "id": str(org.id),
            "username": org.username or "",
            "name": org.name,
            "logo": profile.logo if profile else "",
            "type": org.type,
            "level": profile.level if profile else "",
            "city": primary_location.city if primary_location else "",
            "is_verified": org.is_verified,
            "followers_count": profile.followers_count if profile else 0,
        }
