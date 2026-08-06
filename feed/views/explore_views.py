import logging, uuid
from rest_framework import status
from rest_framework.exceptions import NotFound

from core.views.base_views import BaseAPIView
from organization.models import Organization
from sports.models import Sport, SportPosition
from posts.serializers.posts_serializers import PostListSerializer
from posts.services.saved_post_service import annotate_is_saved
from utils.response import response_data
from utils.validations import is_valid_uuid
from highlights.selectors.highlight_selectors import (
    visible_highlight_counts_for,
)
from feed.services.explore_services import ExploreService
from feed.services.feed_services import FeedService
from feed.pagination import FeedCursorPagination
from feed.serializers.explore_serializers import (
    ExploreUserSerializer, ExploreOrgSerializer,
)

logger = logging.getLogger(__name__)

# Bounds for the search / geo params (kept here so both views agree).
SEARCH_MAX_LEN = 100
RADIUS_DEFAULT_KM = 50.0
RADIUS_MIN_KM = 1.0
RADIUS_MAX_KM = 500.0


def parse_explore_filters(request, *, allow_position):
    """
    Parse + validate the shared Explore listing filters. Returns
    ``(filters, error)``: on failure ``error`` is a message string (the caller
    returns it as a 400) and ``filters`` is None; on success ``error`` is None.

    ``allow_position`` gates the players-only ``position_id`` param.
    """
    filters = {
        "search": None,
        "sport_id": None,
        "position_id": None,
        "lat": None,
        "lng": None,
        "radius_km": None,
        "mode": None,
    }

    # ── search ── trim, drop if empty, cap length ─────────────────
    search = (request.query_params.get("search") or "").strip()
    if search:
        filters["search"] = search[:SEARCH_MAX_LEN]

    # ── mode ── explicit rail mode override ("nearby" | "popular") ──
    # Lets a rail request one discovery mode instead of the auto
    # nearby-else-popular pick. The org explore page uses this to show
    # "Popular players" and "Players near you" as two distinct rails.
    mode = (request.query_params.get("mode") or "").strip().lower()
    if mode:
        if mode not in ("nearby", "popular"):
            return None, "Invalid mode"
        filters["mode"] = mode

    # ── sport_id ── valid UUID + Sport must exist ─────────────────
    sport_id = (request.query_params.get("sport_id") or "").strip()
    if sport_id:
        if not is_valid_uuid(sport_id):
            return None, "Invalid sport_id"
        if not Sport.objects.filter(id=sport_id).exists():
            return None, "Sport not found"
        filters["sport_id"] = sport_id

    # ── position_id ── players only, requires sport_id, must belong to it ──
    position_id = (request.query_params.get("position_id") or "").strip()
    if position_id:
        if not allow_position:
            return None, "position_id is not supported for this listing"
        if not filters["sport_id"]:
            return None, "position_id requires sport_id"
        if not is_valid_uuid(position_id):
            return None, "Invalid position_id"
        position = SportPosition.objects.filter(id=position_id).first()
        if not position:
            return None, "Position not found"
        if position.sport_id != uuid.UUID(filters["sport_id"]):
            return None, "Position does not belong to the selected sport"
        filters["position_id"] = position_id

    # ── lat / lng ── must come together, valid ranges ────────────
    lat_raw = request.query_params.get("lat")
    lng_raw = request.query_params.get("lng")
    has_lat = lat_raw is not None and lat_raw.strip() != ""
    has_lng = lng_raw is not None and lng_raw.strip() != ""
    if has_lat != has_lng:
        return None, "lat and lng must be provided together"

    if has_lat and has_lng:
        try:
            lat = float(lat_raw)
            lng = float(lng_raw)
        except (TypeError, ValueError):
            return None, "lat and lng must be valid numbers"
        if not (-90.0 <= lat <= 90.0):
            return None, "lat must be between -90 and 90"
        if not (-180.0 <= lng <= 180.0):
            return None, "lng must be between -180 and 180"
        filters["lat"] = lat
        filters["lng"] = lng

        # radius is only meaningful alongside a location filter.
        radius_raw = request.query_params.get("radius_km")
        if radius_raw is not None and radius_raw.strip() != "":
            try:
                radius = float(radius_raw)
            except (TypeError, ValueError):
                return None, "radius_km must be a valid number"
        else:
            radius = RADIUS_DEFAULT_KM
        filters["radius_km"] = max(RADIUS_MIN_KM, min(radius, RADIUS_MAX_KM))

    return filters, None


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

            filters, error = parse_explore_filters(request, allow_position=True)
            if error:
                return response_data(success=False, message=error, status_code=400)

            result = ExploreService.discover_players(actor, request, filters)

            serializer = ExploreUserSerializer(
                result["results"],
                many=True,
                context={
                    "followed_user_ids": result["followed_user_ids"],
                    "followed_org_ids": result["followed_org_ids"],
                    # One grouped query for the page's chips — see
                    # highlights.selectors.visible_highlight_counts_for.
                    "highlight_counts": visible_highlight_counts_for(
                        [player.id for player in result["results"]],
                        actor,
                    ),
                },
            )

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

            # ── shared search / sport / location filters ──────────────
            filters, error = parse_explore_filters(request, allow_position=False)
            if error:
                return response_data(success=False, message=error, status_code=400)

            result = ExploreService.discover_organizations(
                actor, request, types, filters
            )

            serializer = ExploreOrgSerializer(
                result["results"],
                many=True,
                context={
                    "followed_user_ids": result["followed_user_ids"],
                    "followed_org_ids": result["followed_org_ids"],
                },
            )

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
            queryset = annotate_is_saved(
                ExploreService.get_trending_posts_queryset(actor, seen_ids=seen_ids),
                actor,
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
