import logging
from rest_framework import status
from rest_framework.exceptions import NotFound

from core.views.base_views import BaseAPIView
from posts.serializers.posts_serializers import PostListSerializer
from posts.services.post_search_service import PostSearchService
from posts.pagination import PostSearchCursorPagination
from feed.services.feed_services import FeedService
from utils.response import response_data

logger = logging.getLogger(__name__)

# Max length of the search query, kept in line with the explore search cap.
SEARCH_MAX_LEN = 100


class PostSearchAPIView(BaseAPIView):
    """
    GET /posts/search?q=<query>&cursor=<cursor>

    Global search over public, live posts. Matches a post when its content
    contains ``q`` OR it has a hashtag whose name contains ``q`` (a leading "#"
    in the query is stripped for the hashtag match). Results are strictly
    newest-first — no scoring, no per-author diversification — so finding your
    own post by its text is expected (search is global, unlike the feed).
    """

    def get(self, request):
        TAG = "PostSearchAPIView"
        try:
            actor = request.actor

            # Actor guard — same as the explore views (user or org actors only).
            if not actor or (not actor.is_user and not actor.is_org):
                return response_data(
                    False,
                    "Search is only available for users and organizations",
                    status_code=400,
                )

            # q — required, trimmed, capped. Empty after trim → 400.
            q = (request.query_params.get("q") or "").strip()
            if not q:
                return response_data(
                    success=False,
                    message="Search query 'q' is required",
                    status_code=400,
                )
            q = q[:SEARCH_MAX_LEN]

            # 1. QUERYSET
            queryset = PostSearchService.search_posts_queryset(q)

            # 2. PAGINATION (keyset on -id, page size 15)
            paginator = PostSearchCursorPagination()
            paginated_posts = paginator.paginate_queryset(queryset, request)

            post_ids = [p.id for p in paginated_posts]

            # 3. ACTOR REACTIONS
            user_reactions = FeedService.get_actor_reactions(actor, post_ids)

            # 4. SERIALIZE — identical item shape to the trending feed.
            serializer = PostListSerializer(
                paginated_posts,
                many=True,
                context={"user_reactions": user_reactions},
            )

            return response_data(
                success=True,
                message="Posts fetched successfully",
                data={
                    "next_cursor": paginator.get_next_cursor(),
                    "results": serializer.data,
                },
            )

        except NotFound as e:
            # Malformed pagination cursor.
            return response_data(
                success=False,
                message=str(e.detail),
                status_code=400,
            )

        except Exception as e:
            logger.error(f"{TAG} | Error | {str(e)}")
            return response_data(
                success=False,
                message="Something went wrong",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                error=str(e),
            )
