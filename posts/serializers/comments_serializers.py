from rest_framework import serializers
from posts.models import Post, PostMedia, Comment
from accounts.serializers.user_serializers import UserMiniSerializer
from organization.serializers.organization_serializers import OrganizationMiniSerializer
from sports.serializers.sports_serializers import SportSerializer
from core.constant import TYPE_USER, TYPE_ORGANIZATION


class CommentReplySerializer(serializers.ModelSerializer):
    actor = serializers.SerializerMethodField()
    reply_to = serializers.SerializerMethodField()

    class Meta:
        model = Comment
        fields = ["id", "comment", "actor", "reply_to", "created_at"]

    def get_actor(self, obj):
        if obj.user:
            return UserMiniSerializer(obj.user).data
        return OrganizationMiniSerializer(obj.organization).data

    def get_reply_to(self, obj):
        if not obj.reply_to:
            return None

        if obj.reply_to.user:
            return UserMiniSerializer(obj.reply_to.user).data
        return OrganizationMiniSerializer(obj.reply_to.organization).data


class CommentSerializer(serializers.ModelSerializer):
    actor = serializers.SerializerMethodField()
    actor_type = serializers.SerializerMethodField()
    replies_count = serializers.IntegerField(source="reply_count")
    replies_preview = serializers.SerializerMethodField()

    class Meta:
        model = Comment
        fields = [
            "id",
            "comment",
            "created_at",
            "actor",
            "actor_type",
            "replies_count",
            "replies_preview",
        ]

    def get_actor(self, obj):
        if obj.user:
            return UserMiniSerializer(obj.user).data
        return OrganizationMiniSerializer(obj.organization).data

    def get_actor_type(self, obj):
        return TYPE_USER if obj.user else TYPE_ORGANIZATION

    def get_replies_preview(self, obj):
        replies = getattr(obj, "all_replies", [])[:2]
        return CommentReplySerializer(replies, many=True).data