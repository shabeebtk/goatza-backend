from rest_framework import serializers
from accounts.serializers.user_serializers import UserMiniSerializer
from organization.serializers.organization_serializers import OrganizationMiniSerializer
from core.constant import TYPE_USER, TYPE_ORGANIZATION


class LikeListSerializer(serializers.Serializer):
    actor = serializers.SerializerMethodField()
    actor_type = serializers.SerializerMethodField()
    liked_at = serializers.DateTimeField(source="created_at")

    def get_actor(self, obj):
        if obj.user:
            return UserMiniSerializer(obj.user).data
        return OrganizationMiniSerializer(obj.organization).data

    def get_actor_type(self, obj):
        return TYPE_USER if obj.user else TYPE_ORGANIZATION