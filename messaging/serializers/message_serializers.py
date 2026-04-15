from rest_framework import serializers
from messaging.models import Message
from accounts.serializers.user_serializers import UserMiniSerializer


class MessageSerializer(serializers.ModelSerializer):

    sender_user = UserMiniSerializer(read_only=True)
    sender_id = serializers.UUIDField(source="sender_user_id", read_only=True)

    class Meta:
        model = Message
        fields = [
            "id",
            "content",
            "message_type",
            "media_url",
            "created_at",
            "sender_user",
            "sender_id",
        ]