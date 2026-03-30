import logging
from django.db.models import Q
from rest_framework import status

from core.views.base_views import BaseAPIView
from posts.models import Post
from connections.services.follow_services import FollowService
from posts.serializers.posts_serializers import PostListSerializer
from posts.models import Like
from utils.response import response_data

logger = logging.getLogger(__name__)


class FeedAPIView(BaseAPIView):

    def get(self, request):
        TAG = "FeedAPIView"

        try:
            actor = request.actor

            # -------------------------
            # 🔹 Cursor pagination
            # -------------------------
            cursor = request.query_params.get("cursor")  # ISO datetime
            limit = int(request.query_params.get("limit", 20))

            limit = min(limit, 50)

            # -------------------------
            # 🔥 FOLLOWING IDS (CORE)
            # -------------------------
            following_data = FollowService.get_following_ids(actor)

            following_user_ids = following_data["user_ids"]
            following_org_ids = following_data["org_ids"]

            # -------------------------
            # 🔥 BASE QUERY
            # -------------------------
            queryset = Post.objects.filter(
                is_deleted=False
            )

            # -------------------------
            # 🔒 VISIBILITY
            # -------------------------
            queryset = queryset.filter(
                Q(visibility=Post.Visibility.PUBLIC) |
                Q(
                    visibility=Post.Visibility.FOLLOWERS,
                    author_user_id__in=following_user_ids
                ) |
                Q(
                    visibility=Post.Visibility.FOLLOWERS,
                    author_org_id__in=following_org_ids
                ) |
                Q(author_user=actor.user if actor.is_user else None) |
                Q(author_org=actor.organization if actor.is_org else None)
            )

            # -------------------------
            # 🧠 FOLLOWED AUTHORS ONLY (FEED CORE)
            # -------------------------
            queryset = queryset.filter(
                Q(author_user_id__in=following_user_ids) |
                Q(author_org_id__in=following_org_ids) |
                Q(author_user=actor.user if actor.is_user else None) |
                Q(author_org=actor.organization if actor.is_org else None)
            )

            # -------------------------
            # ⏱️ CURSOR FILTER
            # -------------------------
            if cursor:
                queryset = queryset.filter(created_at__lt=cursor)

            # -------------------------
            # ⚡ OPTIMIZATION
            # -------------------------
            queryset = queryset.select_related(
                "author_user__profile",
                "author_org",
                "sport"
            ).prefetch_related("media")

            queryset = queryset.order_by("-created_at")[:limit]

            # -------------------------
            # ❤️ LIKED POSTS
            # -------------------------
            post_ids = [p.id for p in queryset]

            if actor.is_user:
                liked_post_ids = set(
                    Like.objects.filter(
                        user=actor.user,
                        post_id__in=post_ids
                    ).values_list("post_id", flat=True)
                )
            else:
                liked_post_ids = set()

            # -------------------------
            # 🧾 SERIALIZE
            # -------------------------
            serializer = PostListSerializer(
                queryset,
                many=True,
                context={"liked_post_ids": liked_post_ids}
            )

            # -------------------------
            # 🔁 NEXT CURSOR
            # -------------------------
            next_cursor = None
            if queryset:
                next_cursor = queryset[-1].created_at.isoformat()

            logger.info(f"{TAG} | count={len(serializer.data)}")

            return response_data(
                success=True,
                data={
                    "results": serializer.data,
                    "next_cursor": next_cursor
                }
            )

        except Exception as e:
            logger.error(f"{TAG} | Error | {str(e)}")

            return response_data(
                success=False,
                message="Something went wrong",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                error=str(e)
            )