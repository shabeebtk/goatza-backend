import logging, uuid
from rest_framework import status
from rest_framework.exceptions import NotFound
from rest_framework.response import Response

from core.views.base_views import BaseAPIView
from posts.serializers.posts_serializers import PostListSerializer
from utils.response import response_data
from feed.services.feed_services import FeedService
from feed.services.impression_services import FeedImpressionService
from feed.services.ranking_services import FeedRankingService
from feed.throttles import FeedImpressionThrottle

logger = logging.getLogger(__name__)


class FeedAPIView(BaseAPIView):
    """
    GET /feed/list

    The ranked home feed. Candidates are blended in SQL (§3.4) and scored with
    the gravity decay (§3.1); the seen-penalty, session jitter and author cap
    run in Python over the whole 300-row window (§3.2, 3.3, 3.5). See
    ``FeedRankingService`` for why this cannot be keyset pagination.
    """

    MAX_SEEN_IDS = 30

    def get(self, request):
        TAG = "FeedAPIView"

        try:
            actor = request.actor

            if not actor or (not actor.is_user and not actor.is_org):
                return response_data(
                    False,
                    "Feed is only available for users and organizations",
                    status_code=400
                )

            seen_ids = self._parse_seen_ids(
                request.query_params.get("seen_ids")
            )

            # 1. RANKED PAGE (ranking cached per session; see ranking_services)
            page = FeedRankingService.get_page(
                actor,
                request.user,
                cursor=request.query_params.get("cursor"),
                seen_ids=seen_ids,
            )
            posts = page["posts"]

            # 2. ACTOR REACTIONS
            user_reactions = FeedService.get_actor_reactions(
                actor, [post.id for post in posts]
            )

            # 3. SERIALIZE
            serializer = PostListSerializer(
                posts,
                many=True,
                context={"user_reactions": user_reactions}
            )

            return response_data(
                success=True,
                message="Feed fetched successfully",
                data={
                    "next_cursor": page["next_cursor"],
                    "results": serializer.data
                }
            )

        except NotFound as e:
            # Malformed pagination cursor — same handling as the explore views.
            return response_data(
                success=False,
                message=str(e.detail),
                status_code=400,
            )

        except Exception as e:
            logger.error(f"{TAG} | Error | {str(e)}", exc_info=True)

            return response_data(
                success=False,
                message="Something went wrong",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                error=str(e)
            )

    def _parse_seen_ids(self, raw):
        """
        Comma-separated uuids from the client, capped. A malformed token voids
        the whole param (unchanged behaviour) — it is only a de-duplication
        hint, so being wrong about it costs a repeated post, not an error.
        """
        if not raw:
            return []

        try:
            seen_ids = [
                uuid.UUID(sid.strip())
                for sid in raw.split(",")
                if sid.strip()
            ]
            return seen_ids[:self.MAX_SEEN_IDS]
        except Exception:
            return []


class FeedImpressionsAPIView(BaseAPIView):
    """
    POST /feed/impressions  {"post_ids": [...]}

    Records what the reader has actually seen (§3.2). Fire-and-forget: junk is
    ignored rather than rejected, and the response is an empty 204 so the client
    has nothing to parse and nothing to show.

    Throttled on its own scope so a scroll's telemetry cannot drain the shared
    per-user bucket that real actions (posting, liking, messaging) share.
    """

    throttle_classes = [FeedImpressionThrottle]

    def post(self, request):
        TAG = "FeedImpressionsAPIView"

        try:
            post_ids = FeedImpressionService.parse_post_ids(
                request.data.get("post_ids")
            )

            if post_ids:
                FeedImpressionService.record(request.user, post_ids)

            # Retention, inline — there is no nightly job to do it.
            FeedImpressionService.maybe_prune(request.user)

            return Response(status=status.HTTP_204_NO_CONTENT)

        except Exception as e:
            # Even a genuine failure returns 204: the reader gets nothing out of
            # knowing, and the client is explicitly built to ignore the result.
            logger.error(f"{TAG} | Error | {str(e)}", exc_info=True)
            return Response(status=status.HTTP_204_NO_CONTENT)
