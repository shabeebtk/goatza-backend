from rest_framework import serializers

# Mirrors ShareRequestSerializer.NOTE_MAX_LENGTH — the caption on a photo.
CAPTION_MAX_LENGTH = 1000


class SendImageMessageSerializer(serializers.Serializer):
    """
    Body for POST /conversations/<id>/messages/media (media_type=image).

    Only shape/format is enforced here; the SECURITY checks (URL is from our
    cloud, in the sender's chat folder, size cap) live in
    MessageService._validate_chat_image so they can't be bypassed by another
    caller.
    """
    media_url = serializers.URLField(max_length=500)
    media_public_id = serializers.CharField(max_length=255)

    width = serializers.IntegerField(min_value=0, required=False, allow_null=True)
    height = serializers.IntegerField(min_value=0, required=False, allow_null=True)
    size_bytes = serializers.IntegerField(
        min_value=0, required=False, allow_null=True
    )

    caption = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
        max_length=CAPTION_MAX_LENGTH,
    )


class SendVideoMessageSerializer(SendImageMessageSerializer):
    """
    Body for POST /conversations/<id>/messages/media (media_type=video). Adds
    the client-reported duration; the thumbnail is derived server-side.
    """
    duration_ms = serializers.IntegerField(
        min_value=0, required=False, allow_null=True
    )
