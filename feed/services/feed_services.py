# feed/services/feed.service.py

from django.db.models import Case, When, Value, FloatField, F, Q
from django.db.models.functions import Ln, Cast
from django.db.models import ExpressionWrapper, DurationField
from collections import defaultdict, deque
from django.utils.timezone import now
from datetime import timedelta

from posts.models import Post, Like
from connections.models import Follow
from sports.models import UserSport


class FeedService:

    MAX_POSTS_PER_AUTHOR = 3

    @staticmethod
    def get_feed_queryset(user, seen_ids=None):
        """
        Build optimized feed queryset with scoring
        """

        # 1. FOLLOWING USERS
        following_users = Follow.objects.filter(
            follower_user=user
        ).values_list("following_user_id", flat=True)

        # 2. USER SPORTS
        user_sports = UserSport.objects.filter(
            user=user
        ).values_list("sport_id", flat=True)

        primary_sports = UserSport.objects.filter(
            user=user,
            is_primary=True
        ).values_list("sport_id", flat=True)

        # 3. BASE QUERY + VISIBILITY
        queryset = Post.objects.filter(is_deleted=False)

        visibility_filter = Q(visibility=Post.Visibility.PUBLIC)

        visibility_filter |= Q(
            visibility=Post.Visibility.FOLLOWERS,
            author_user__in=following_users
        )

        visibility_filter |= Q(author_user=user)

        queryset = queryset.filter(visibility_filter)

        if seen_ids:
            queryset = queryset.exclude(id__in=seen_ids)

        # 4. SCORING
        queryset = queryset.annotate(

            follow_score=Case(
                When(author_user__in=following_users, then=Value(6)),
                default=Value(0),
                output_field=FloatField()
            ),

            primary_interest_score=Case(
                When(sport__in=primary_sports, then=Value(5)),
                default=Value(0),
                output_field=FloatField()
            ),

            secondary_interest_score=Case(
                When(sport__in=user_sports, then=Value(3)),
                default=Value(0),
                output_field=FloatField()
            ),

            engagement_score=Ln(F("likes_count") + F("comments_count") + 1),

            # simple recency boost
            recency_boost=Case(
                When(created_at__gte=now() - timedelta(hours=2), then=Value(5)),
                When(created_at__gte=now() - timedelta(hours=24), then=Value(2)),
                default=Value(0),
                output_field=FloatField()
            )
        )

        queryset = queryset.annotate(
            final_score=
                F("follow_score") +
                F("primary_interest_score") +
                F("secondary_interest_score") +
                F("engagement_score") * 2 +
                F("recency_boost")
        ).order_by("-final_score", "-created_at")

        return queryset.select_related(
            "author_user__profile",
            "author_org",
            "sport"
        ).prefetch_related("media")


    # REACTIONS HELPER
    @staticmethod
    def get_user_reactions(user, post_ids):
        reactions = Like.objects.filter(
            user=user,
            post_id__in=post_ids
        ).values("post_id", "type")

        return {
            r["post_id"]: r["type"]
            for r in reactions
        }



    @staticmethod
    def diversify_posts(posts):
        """
        Interleave posts from different authors
        """
        author_buckets = defaultdict(deque)

        for post in posts:
            author_id = post.author_user_id or f"org_{post.author_org_id}"
            author_buckets[author_id].append(post)

        diversified = []

        while author_buckets:
            for author_id in list(author_buckets.keys()):
                if author_buckets[author_id]:
                    diversified.append(author_buckets[author_id].popleft())
                if not author_buckets[author_id]:
                    del author_buckets[author_id]

        return diversified