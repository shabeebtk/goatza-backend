# notifications/serializers.py
from rest_framework import serializers
from notifications.models import Notification
from notifications.services.deeplink_service import build_notification_url


class NotificationSerializer(serializers.ModelSerializer):
    actor_name = serializers.SerializerMethodField()
    actor_avatar = serializers.SerializerMethodField()
    actor_username = serializers.SerializerMethodField()
    actor_type = serializers.SerializerMethodField()
    post_id = serializers.SerializerMethodField()
    # Same resolver the grouped shape and the push payload use. Carried here too
    # so the ungrouped response can't quietly drift from the grouped one.
    url = serializers.SerializerMethodField()

    class Meta:
        model = Notification
        fields = [
            "id",
            "type",
            "actor_name",
            "actor_avatar",
            "actor_username",
            "actor_type",
            "post_id",
            "url",
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

    def get_actor_username(self, obj):
        if obj.actor_user:
            return obj.actor_user.username
        
        if obj.actor_org:
            return str(obj.actor_org.username)
        return None

    def get_actor_avatar(self, obj):
        if obj.actor_user:
            return getattr(obj.actor_user.profile, "profile_photo", None)

        if obj.actor_org:
            return getattr(obj.actor_org.profile, "logo", None)  
        return None
    

    def get_actor_type(self, obj):
        if obj.actor_user:
            return "user"
        if obj.actor_org:
            return "organization"
        return None

    def get_url(self, obj):
        return build_notification_url(obj)

    def get_post_id(self, obj):
        if obj.post:
            return str(obj.post.id)
        # fallback to data payload if needed
        return obj.data.get("post_id", None)