from rest_framework import serializers
from organization.serializers.organization_serializers import OrganizationMiniSerializer
from accounts.serializers.user_serializers import UserMiniSerializer


class ActorMiniSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    username = serializers.CharField()
    name = serializers.CharField()
    avatar = serializers.CharField(allow_blank=True)
    type = serializers.CharField()

    def to_representation(self, obj):
        # USER
        if hasattr(obj, "profile"):
            return {
                "id": str(obj.id),
                "username": obj.username,
                "name": getattr(obj.profile, "name", ""),
                "avatar": getattr(obj.profile, "profile_photo", ""),
                "type": "user",
            }

        # ORGANIZATION
        return {
            "id": str(obj.id),
            "username": obj.username,
            "name": obj.name,
            "avatar": getattr(obj.profile, "logo", ""),
            "type": "organization",
        }