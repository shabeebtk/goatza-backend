from django.db.models import Prefetch
from rest_framework import serializers
from posts.models import Post, PostMedia, PostMention, Comment
from accounts.serializers.user_serializers import UserMiniSerializer
from organization.serializers.organization_serializers import OrganizationMiniSerializer
from sports.serializers.sports_serializers import SportSerializer
from core.constant import TYPE_USER, TYPE_ORGANIZATION

# Every queryset that feeds PostListSerializer must carry this, or `mentions`
# costs three queries per post (the rows, then each target). Defined once here
# so the feed, explore, profile list, search, post detail and the mentions list
# can't drift apart.
POST_MENTIONS_PREFETCH = Prefetch(
    "mentions",
    queryset=PostMention.objects.select_related("mentioned_user", "mentioned_org"),
)


class PostMediaSerializer(serializers.ModelSerializer):
    class Meta:
        model = PostMedia
        fields = [
            # The client needs the media id to promote a post video into
            # highlights (`source_media_id`) — see highlights/services.
            "id",
            "file_url",
            "media_type",
            "thumbnail_url",
            "duration",
            "width",
            "height",
            "order"
        ]


class PostListSerializer(serializers.ModelSerializer):
    author = serializers.SerializerMethodField()
    author_type = serializers.SerializerMethodField()
    media = PostMediaSerializer(many=True, read_only=True)
    sport = SportSerializer(read_only=True)
    reaction = serializers.SerializerMethodField()
    location = serializers.SerializerMethodField()
    mentions = serializers.SerializerMethodField()

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
            "location",
            "mentions",
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
            "is_reacted": reaction_type is not None,
            "type": reaction_type
        }

    def get_location(self, obj):
        if not obj.latitude:
            return None

        return {
            "name": obj.location_name,
            "city": obj.city,
            "country_code": obj.country_code,
            "latitude": obj.latitude,
            "longitude": obj.longitude,
    }

    def get_mentions(self, obj):
        """
        Just enough for the client to linkify @handles in `content` and route
        each to the right profile — the full actor is one tap away.
        Reads the POST_MENTIONS_PREFETCH cache, so it issues no queries.
        """
        mentions = []
        for mention in obj.mentions.all():
            if mention.mentioned_user_id:
                mentions.append({
                    "username": mention.mentioned_user.username or "",
                    "type": TYPE_USER,
                })
            elif mention.mentioned_org_id:
                mentions.append({
                    "username": mention.mentioned_org.username or "",
                    "type": TYPE_ORGANIZATION,
                })
        return mentions
    


class PostMiniSerializer(serializers.ModelSerializer):
    author = serializers.SerializerMethodField()
    media = serializers.SerializerMethodField()

    class Meta:
        model = Post
        fields = [
            "id",
            "content",
            "media",
            "author"
        ]

    def get_author(self, obj):
        if obj.author_user:
            data = UserMiniSerializer(obj.author_user).data
            data["type"] = "user"
            return data

        data = OrganizationMiniSerializer(obj.author_org).data
        data["type"] = "organization"
        return data
    
    
    def get_media(self, obj):
        """
        Return only first media (for notification preview)
        """
        first_media = getattr(obj, "media", None)

        if not first_media:
            return None

        first = first_media.first()
        if not first:
            return None

        return {
            "type": first.media_type,
            "url": first.file_url,
            "thumbnail": first.thumbnail_url,
            "width": first.width,
            "height": first.height,
        }