# notifications/serializers.py
from rest_framework import serializers
from notifications.models import Notification


class NotificationSerializer(serializers.ModelSerializer):
    actor_name = serializers.SerializerMethodField()
    actor_avatar = serializers.SerializerMethodField()

    class Meta:
        model = Notification
        fields = [
            "id",
            "type",
            "actor_name",
            "actor_avatar",
            "data",
            "is_read",
            "created_at",
        ]

    def get_actor_name(self, obj):
        if obj.actor_user:
            return obj.actor_user.profile_name
        if obj.actor_org:
            return obj.actor_org.name
        return None

    def get_actor_avatar(self, obj):
        if obj.actor_user:
            return getattr(obj.actor_user.profile, "profile_photo", None)
        if obj.actor_org:
            return obj.actor_org.logo
        return None