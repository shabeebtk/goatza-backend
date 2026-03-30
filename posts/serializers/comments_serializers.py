from rest_framework import serializers
from posts.models import Post, PostMedia, Comment
from accounts.serializers.user_serializers import UserMiniSerializer
from organization.serializers.organization_serializers import OrganizationMiniSerializer
from sports.serializers.sports_serializers import SportSerializer
from core.constant import TYPE_USER, TYPE_ORGANIZATION


class CommentSerializer(serializers.ModelSerializer):
    actor = serializers.SerializerMethodField()
    actor_type = serializers.SerializerMethodField()
    replies_count = serializers.SerializerMethodField()

    class Meta:
        model = Comment
        fields = [
            "id",
            "comment",
            "created_at",
            "actor",
            "actor_type",
            "parent",
            "replies_count",
        ]

    def get_actor(self, obj):
        if obj.user:
            return UserMiniSerializer(obj.user).data
        return OrganizationMiniSerializer(obj.organization).data

    def get_actor_type(self, obj):
        return TYPE_USER if obj.user else TYPE_ORGANIZATION

    def get_replies_count(self, obj):
        return obj.replies.filter(is_deleted=False).count()