import logging, uuid
from django.db.models import Q
from rest_framework import status

from core.views.base_views import BaseAPIView
from posts.models import Post
from connections.services.follow_services import FollowService
from posts.serializers.posts_serializers import PostListSerializer
from utils.response import response_data
from feed.services.feed_services import FeedService
from feed.pagination import FeedCursorPagination

logger = logging.getLogger(__name__)


class FeedAPIView(BaseAPIView):
    MAX_SEEN_IDS = 30

    def get(self, request):
        TAG = "FeedAPIView"

        try:
            actor = request.actor
            user = request.user
            seen_ids_param = request.query_params.get("seen_ids")

            if not actor or (not actor.is_user and not actor.is_org):
                return response_data(
                    False,
                    "Feed is only available for users and organizations",
                    status_code=400
                )

            seen_ids = []
            if seen_ids_param:
                try:
                    seen_ids = [
                        uuid.UUID(sid.strip())
                        for sid in seen_ids_param.split(",")
                        if sid.strip()
                    ]
                    seen_ids = seen_ids[:self.MAX_SEEN_IDS]
                except Exception:
                    seen_ids = []

            # 1. GET FEED QUERYSET
            queryset = FeedService.get_feed_queryset(actor, seen_ids=seen_ids)

            # 2. PAGINATION
            paginator = FeedCursorPagination()
            paginated_posts = paginator.paginate_queryset(queryset, request)

            # 3. DIVERSIFY POSTS
            paginated_posts = FeedService.diversify_posts(paginated_posts)

            post_ids = [p.id for p in paginated_posts]

            # 4. USER REACTIONS
            user_reactions = FeedService.get_actor_reactions(actor, post_ids)

            # 5. SERIALIZE
            serializer = PostListSerializer(
                paginated_posts,
                many=True,
                context={"user_reactions": user_reactions}
            )

            # 6. RESPONSE
            return response_data(
                success=True,
                message="Feed fetched successfully",
                data={
                    "next_cursor": paginator.get_next_cursor(),
                    "results": serializer.data
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