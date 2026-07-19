from rest_framework import serializers

from core.constant import TYPE_ORGANIZATION, TYPE_USER
from messaging.services.share_service import TARGET_POST, TARGET_RECRUITMENT

# One share call may fan out to at most this many existing threads, and open at
# most this many new ones. Caps the blast radius of a single request; the
# per-actor throttle caps the rate.
MAX_CONVERSATIONS = 10
MAX_RECIPIENTS = 10

NOTE_MAX_LENGTH = 1000


class ShareTargetSerializer(serializers.Serializer):
    type = serializers.ChoiceField(choices=[TARGET_POST, TARGET_RECRUITMENT])
    id = serializers.UUIDField()


class ShareRecipientSerializer(serializers.Serializer):
    """A person/org to share with who may not have a conversation yet."""
    actor_type = serializers.ChoiceField(choices=[TYPE_USER, TYPE_ORGANIZATION])
    actor_id = serializers.UUIDField()


class ShareRequestSerializer(serializers.Serializer):
    target = ShareTargetSerializer()

    conversation_ids = serializers.ListField(
        child=serializers.UUIDField(),
        required=False,
        default=list,
        max_length=MAX_CONVERSATIONS,
    )

    recipients = ShareRecipientSerializer(
        many=True,
        required=False,
        default=list,
    )

    note = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
        max_length=NOTE_MAX_LENGTH,
    )

    def validate_recipients(self, value):
        if len(value) > MAX_RECIPIENTS:
            raise serializers.ValidationError(
                f"At most {MAX_RECIPIENTS} recipients per share"
            )
        return value

    def validate(self, attrs):
        if not attrs.get("conversation_ids") and not attrs.get("recipients"):
            raise serializers.ValidationError(
                "Provide at least one conversation_id or recipient"
            )
        return attrs
