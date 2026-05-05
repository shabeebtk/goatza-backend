from rest_framework import serializers
from messaging.models import Message
from shared.serializers.actor_serializers import ActorMiniSerializer

class MessageSerializer(serializers.ModelSerializer):
    sender = serializers.SerializerMethodField()

    class Meta:
        model = Message
        fields = [
            "id",
            "content",
            "message_type",
            "media_url",
            "created_at",
            "sender",
        ]

    def get_sender(self, obj):
        if obj.sender_user:
            return ActorMiniSerializer(obj.sender_user).data

        if obj.sender_org:
            return ActorMiniSerializer(obj.sender_org).data

        return None