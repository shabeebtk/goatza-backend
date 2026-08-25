"""
Request/response shaping for highlights. All the rules stay in the service —
these only shape input and hide the owner-only fields on the way out.
"""

from rest_framework import serializers

from highlights.models import Highlight


class HighlightSerializer(serializers.ModelSerializer):
    """
    One clip as the rail and the viewer render it.

    ``visibility`` and ``views_count`` are the owner's business, so they are
    dropped unless the context says the requester owns the rail. The view sets
    ``is_owner`` once per request — never per row — so a list stays free.
    """

    class Meta:
        model = Highlight
        fields = [
            "id",
            "title",
            "file_url",
            "thumbnail_url",
            "duration",
            "width",
            "height",
            "order",
            "created_at",
            # owner-only, stripped below for everyone else
            "visibility",
            "views_count",
        ]

    def to_representation(self, instance):
        data = super().to_representation(instance)

        if not self.context.get("is_owner"):
            data.pop("visibility", None)
            data.pop("views_count", None)

        return data


class HighlightCreateSerializer(serializers.Serializer):
    """
    Create input, in either mode:

      * promote — ``source_media_id`` (a PostMedia of the player's own video post)
      * direct  — ``file_url`` + ``public_id`` from the presigned direct upload

    Shape only. Ownership, the 10-clip cap, the 90s cap and the media checks
    all belong to HighlightService and run there.
    """

    source_media_id = serializers.UUIDField(required=False)

    file_url = serializers.URLField(required=False)
    public_id = serializers.CharField(required=False, max_length=255)
    thumbnail_url = serializers.URLField(required=False, allow_blank=True)
    duration = serializers.IntegerField(required=False, min_value=0)
    width = serializers.IntegerField(required=False, min_value=0)
    height = serializers.IntegerField(required=False, min_value=0)

    title = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=Highlight._meta.get_field("title").max_length,
    )
    visibility = serializers.ChoiceField(
        choices=Highlight.Visibility.choices,
        required=False,
    )

    def validate(self, attrs):
        if not attrs.get("source_media_id") and not attrs.get("file_url"):
            raise serializers.ValidationError(
                "Send either source_media_id to promote a post video, or "
                "file_url and public_id for a direct upload."
            )

        if not attrs.get("source_media_id") and not attrs.get("public_id"):
            raise serializers.ValidationError(
                "The uploaded video id (public_id) is required."
            )

        return attrs


class HighlightUpdateSerializer(serializers.Serializer):
    """PATCH body — send whichever of the two is changing."""

    title = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=Highlight._meta.get_field("title").max_length,
    )
    visibility = serializers.ChoiceField(
        choices=Highlight.Visibility.choices,
        required=False,
    )

    def validate(self, attrs):
        if not attrs:
            raise serializers.ValidationError(
                "Nothing to update. Send a title or a visibility."
            )
        return attrs


class HighlightReorderSerializer(serializers.Serializer):
    """PUT body — the full rail, in its new order."""

    ordered_ids = serializers.ListField(
        child=serializers.UUIDField(),
        allow_empty=True,
    )
