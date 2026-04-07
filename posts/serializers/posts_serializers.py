from rest_framework import serializers
from posts.models import Post, PostMedia, Comment
from accounts.serializers.user_serializers import UserMiniSerializer
from organization.serializers.organization_serializers import OrganizationMiniSerializer
from sports.serializers.sports_serializers import SportSerializer
from core.constant import TYPE_USER, TYPE_ORGANIZATION

class PostMediaSerializer(serializers.ModelSerializer):
    class Meta:
        model = PostMedia
        fields = [
            "file_url",
            "media_type",
            "thumbnail_url",
            "duration",
            "order"
        ]


class PostListSerializer(serializers.ModelSerializer):
    author = serializers.SerializerMethodField()
    author_type = serializers.SerializerMethodField()
    media = PostMediaSerializer(many=True, read_only=True)
    sport = SportSerializer(read_only=True)
    reaction = serializers.SerializerMethodField()

    class Meta:
        model = Post
        fields = [
            "id",
            "content",
            "post_type",
            "visibility",
            "likes_count",
            "likes_breakdown",
            "comments_count",
            "created_at",
            "author",
            "author_type",
            "media",
            "sport",
            "reaction",
        ]

    def get_author(self, obj):
        if obj.author_user:
            return UserMiniSerializer(obj.author_user).data
        return OrganizationMiniSerializer(obj.author_org).data

    def get_author_type(self, obj):
        return TYPE_USER if obj.author_user else TYPE_ORGANIZATION

    def get_reaction(self, obj):
        user_reactions = self.context.get("user_reactions", {})
        reaction_type = user_reactions.get(obj.id)
        return {
            "is_reacted": bool(reaction_type),
            "type": reaction_type  # "like" | "fire" | "respect"
        }
    


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