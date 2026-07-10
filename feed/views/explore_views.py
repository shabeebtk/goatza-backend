import logging, uuid
from rest_framework import status
from rest_framework.exceptions import NotFound

from core.views.base_views import BaseAPIView
from organization.models import Organization
from posts.serializers.posts_serializers import PostListSerializer
from utils.response import response_data
from feed.services.explore_services import ExploreService
from feed.services.feed_services import FeedService
from feed.pagination import FeedCursorPagination
from feed.serializers.explore_serializers import (
    ExploreUserSerializer, ExploreOrgSerializer,
)

logger = logging.getLogger(__name__)


class ExplorePlayersAPIView(BaseAPIView):
    """
    GET /feed/explore/players

    Discover players (role="player") near the current actor. Falls back to a
    most-followed list when the actor has no location or nobody is nearby.
    """

    def get(self, request):
        TAG = "ExplorePlayersAPIView"
        try:
            actor = request.actor

            if not actor or (not actor.is_user and not actor.is_org):
                return response_data(
                    False,
                    "Explore is only available for users and organizations",
                    status_code=400,
                )

            result = ExploreService.discover_players(actor, request)

            serializer = ExploreUserSerializer(result["results"], many=True)

            return response_data(
                success=True,
                message="Players fetched successfully",
                data={
                    "next_cursor": result["next_cursor"],
                    "mode": result["mode"],
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


class ExploreOrganizationsAPIView(BaseAPIView):
    """
    GET /feed/explore/organizations?types=club,team

    Discover organizations near the current actor. Same nearby/popular logic as
    the players endpoint. ``types`` narrows by Organization.Type (all if unset).
    """

    def get(self, request):
        TAG = "ExploreOrganizationsAPIView"
        try:
            actor = request.actor

            if not actor or (not actor.is_user and not actor.is_org):
                return response_data(
                    False,
                    "Explore is only available for users and organizations",
                    status_code=400,
                )

            # ── types filter ──────────────────────────────────────────
            valid_types = set(Organization.Type.values)
            types_param = request.query_params.get("types")

            if types_param:
                requested = [t.strip() for t in types_param.split(",") if t.strip()]
                invalid = [t for t in requested if t not in valid_types]
                if invalid:
                    return response_data(
                        success=False,
                        message=(
                            "Invalid organization type(s): "
                            f"{', '.join(invalid)}. "
                            f"Allowed: {', '.join(sorted(valid_types))}"
                        ),
                        status_code=400,
                    )
                types = requested
            else:
                types = list(valid_types)

            result = ExploreService.discover_organizations(actor, request, types)

            serializer = ExploreOrgSerializer(result["results"], many=True)

            return response_data(
                success=True,
                message="Organizations fetched successfully",
                data={
                    "next_cursor": result["next_cursor"],
                    "mode": result["mode"],
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


class ExploreTrendingPostsAPIView(BaseAPIView):
    """
    GET /feed/explore/posts

    Trending public posts for the Explore page. Same shape as the home feed
    (FeedAPIView): cursor on final_score|id, per-author diversification, and
    ``seen_ids`` for variety between visits — just an engagement-first score
    that mixes in (but deprioritizes) authors the actor already follows.
    """

    MAX_SEEN_IDS = 30

    def get(self, request):
        TAG = "ExploreTrendingPostsAPIView"
        try:
            actor = request.actor

            if not actor or (not actor.is_user and not actor.is_org):
                return response_data(
                    False,
                    "Explore is only available for users and organizations",
                    status_code=400,
                )

            # seen_ids — comma-separated UUIDs, capped, junk silently ignored
            # (identical handling to FeedAPIView).
            seen_ids = []
            seen_ids_param = request.query_params.get("seen_ids")
            if seen_ids_param:
                try:
                    seen_ids = [
                        uuid.UUID(sid.strip())
                        for sid in seen_ids_param.split(",")
                        if sid.strip()
                    ]
                    seen_ids = seen_ids[: self.MAX_SEEN_IDS]
                except Exception:
                    seen_ids = []

            # 1. QUERYSET
            queryset = ExploreService.get_trending_posts_queryset(
                actor, seen_ids=seen_ids
            )

            # 2. PAGINATION (keyset on final_score|id, page size 15)
            paginator = FeedCursorPagination()
            paginated_posts = paginator.paginate_queryset(queryset, request)

            # 3. DIVERSIFY per author (cursor is taken from the score-ordered
            #    page, so this display-only reshuffle can't affect paging).
            paginated_posts = FeedService.diversify_posts(paginated_posts)

            post_ids = [p.id for p in paginated_posts]

            # 4. ACTOR REACTIONS
            user_reactions = FeedService.get_actor_reactions(actor, post_ids)

            # 5. SERIALIZE
            serializer = PostListSerializer(
                paginated_posts,
                many=True,
                context={"user_reactions": user_reactions},
            )

            return response_data(
                success=True,
                message="Trending posts fetched successfully",
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
