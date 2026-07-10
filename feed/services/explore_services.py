# feed/services/explore_services.py

import math
from datetime import timedelta

from django.db.models import (
    Case, When, F, Q, Value, FloatField, ExpressionWrapper, FilteredRelation,
)
from django.db.models.functions import (
    ACos, Cos, Sin, Radians, Least, Greatest, Coalesce, Ln,
)
from django.utils.timezone import now

from accounts.models import User
from organization.models import Organization, OrganizationLocation
from posts.models import Post
from connections.services.follow_services import FollowService
from feed.pagination import ExploreCursorPagination


class ExploreService:
    """
    Read-side query building for the Explore discovery endpoints.

    Two ordering modes, chosen per request (see ``discover_*``):
      - "nearby"  : the actor has coordinates → bounding-box prefilter + a
                    haversine ``distance_km`` annotation, nearest first.
      - "popular" : the actor has no coordinates (or nearby found nobody on the
                    first page) → most-followed first.

    Everything here stays a pure queryset builder; the views wrap the result in
    ``response_data``.
    """

    # Radius (km) of the bounding-box prefilter. The haversine annotation is the
    # authoritative distance — the box just narrows rows using the existing
    # (latitude, longitude) index before the trig runs.
    NEARBY_RADIUS_KM = 150.0

    EARTH_RADIUS_KM = 6371.0

    # ~km per degree of latitude (roughly constant across the globe).
    KM_PER_DEG_LAT = 111.0

    NEARBY = ExploreCursorPagination.NEARBY
    POPULAR = ExploreCursorPagination.POPULAR

    # --- Trending posts (explore) ------------------------------------- #
    # Only posts from this recent a window are eligible to trend.
    TRENDING_WINDOW_DAYS = 30
    # Trending is engagement-first, so engagement is weighted above the home
    # feed's x2 and dominates recency / the followed penalty.
    TRENDING_ENGAGEMENT_WEIGHT = 3
    # Followed authors still surface here, just nudged down the ranking so the
    # explore feed leans toward discovery rather than repeating the home feed.
    TRENDING_FOLLOWED_PENALTY = -2

    # ------------------------------------------------------------------ #
    # LOCATION RESOLUTION
    # ------------------------------------------------------------------ #
    @classmethod
    def resolve_location(cls, actor):
        """
        (latitude, longitude) for the actor, or None when unset.

        user → its profile lat/lng · org → its primary OrganizationLocation.
        """
        if actor.is_user:
            profile = getattr(actor.user, "profile", None)
            if profile and profile.latitude is not None and profile.longitude is not None:
                return (profile.latitude, profile.longitude)
            return None

        primary = (
            OrganizationLocation.objects
            .filter(organization=actor.organization, is_primary=True)
            .values("latitude", "longitude")
            .first()
        )
        if primary and primary["latitude"] is not None and primary["longitude"] is not None:
            return (primary["latitude"], primary["longitude"])
        return None

    # ------------------------------------------------------------------ #
    # GEO HELPERS
    # ------------------------------------------------------------------ #
    @classmethod
    def _bounding_box(cls, lat, lng):
        """
        Lat/lng min/max box of NEARBY_RADIUS_KM around (lat, lng). Used as a
        cheap, index-friendly prefilter before the exact haversine distance.
        """
        lat_delta = cls.NEARBY_RADIUS_KM / cls.KM_PER_DEG_LAT

        # Longitude degrees shrink toward the poles; guard cos()→0 so the box
        # never blows up to an infinite width near ±90°.
        cos_lat = max(abs(math.cos(math.radians(lat))), 0.01)
        lng_delta = cls.NEARBY_RADIUS_KM / (cls.KM_PER_DEG_LAT * cos_lat)

        return {
            "min_lat": max(lat - lat_delta, -90.0),
            "max_lat": min(lat + lat_delta, 90.0),
            "min_lng": lng - lng_delta,
            "max_lng": lng + lng_delta,
        }

    @classmethod
    def _distance_expr(cls, lat, lng, lat_field, lng_field):
        """
        Haversine distance (km) from the actor (lat, lng constants) to each row's
        (lat_field, lng_field) as an ORM expression:

            R * acos( sin(lat1)sin(lat2) + cos(lat1)cos(lat2)cos(lng2 - lng1) )

        The acos input is clamped to [-1, 1] (Least/Greatest) so float rounding
        at distance ≈ 0 never trips an acos domain error.
        """
        lat1 = math.radians(lat)
        lng1 = math.radians(lng)
        sin_lat1 = math.sin(lat1)
        cos_lat1 = math.cos(lat1)

        cos_angle = (
            Value(sin_lat1) * Sin(Radians(F(lat_field)))
            + Value(cos_lat1)
            * Cos(Radians(F(lat_field)))
            * Cos(Radians(F(lng_field)) - Value(lng1))
        )
        clamped = Least(Value(1.0), Greatest(Value(-1.0), cos_angle))

        return ExpressionWrapper(
            ACos(clamped) * Value(cls.EARTH_RADIUS_KM),
            output_field=FloatField(),
        )

    # ------------------------------------------------------------------ #
    # SHARED ORCHESTRATION
    # ------------------------------------------------------------------ #
    @classmethod
    def _discover(cls, actor, request, build_queryset):
        """
        Shared paginate-with-mode flow for both endpoints.

        ``build_queryset(mode, location)`` returns the queryset for a mode.

        - With a cursor: the mode is locked to whatever the cursor carries — no
          re-decision, no fallback (keeps the scroll on one track).
        - No cursor: pick "nearby" when the actor has a location, else "popular".
          If nearby's first page is empty, fall back to "popular" once.
        """
        location = cls.resolve_location(actor)
        paginator = ExploreCursorPagination()
        cursor = request.query_params.get("cursor")

        if cursor:
            # Raises NotFound on a malformed cursor (handled by the view).
            mode = paginator.decode_mode(cursor)
        else:
            mode = cls.NEARBY if location else cls.POPULAR

        queryset = build_queryset(mode, location)
        page = paginator.paginate_queryset(queryset, request, mode)

        # First-page nearby found nobody → switch to popular for this request.
        if not cursor and mode == cls.NEARBY and not page:
            mode = cls.POPULAR
            queryset = build_queryset(mode, location)
            page = paginator.paginate_queryset(queryset, request, mode)

        return {
            "results": page,
            "mode": mode,
            "next_cursor": paginator.get_next_cursor(),
        }

    # ------------------------------------------------------------------ #
    # PLAYERS
    # ------------------------------------------------------------------ #
    @classmethod
    def discover_players(cls, actor, request):
        followed = FollowService.get_following_ids(actor)

        def build_queryset(mode, location):
            return cls._players_queryset(actor, mode, location, followed)

        return cls._discover(actor, request, build_queryset)

    @classmethod
    def _players_queryset(cls, actor, mode, location, followed):
        # Base pool: players that actually have a profile.
        queryset = User.objects.filter(
            role=User.Role.PLAYER,
            profile__isnull=False,
        )

        # Never surface the current user or anyone the actor already follows.
        if actor.is_user:
            queryset = queryset.exclude(id=actor.user.id)
        queryset = queryset.exclude(id__in=followed["user_ids"])

        if mode == cls.NEARBY:
            lat, lng = location
            box = cls._bounding_box(lat, lng)
            queryset = (
                queryset
                .filter(
                    profile__latitude__gte=box["min_lat"],
                    profile__latitude__lte=box["max_lat"],
                    profile__longitude__gte=box["min_lng"],
                    profile__longitude__lte=box["max_lng"],
                )
                .annotate(
                    distance_km=cls._distance_expr(
                        lat, lng, "profile__latitude", "profile__longitude"
                    )
                )
                .order_by("distance_km", "id")
            )
        else:  # POPULAR
            queryset = queryset.annotate(
                followers_count=Coalesce(F("profile__followers_count"), Value(0)),
            ).order_by("-followers_count", "-id")

        return queryset.select_related("profile")

    # ------------------------------------------------------------------ #
    # ORGANIZATIONS
    # ------------------------------------------------------------------ #
    @classmethod
    def discover_organizations(cls, actor, request, types):
        followed = FollowService.get_following_ids(actor)

        def build_queryset(mode, location):
            return cls._orgs_queryset(actor, mode, location, followed, types)

        return cls._discover(actor, request, build_queryset)

    @classmethod
    def _orgs_queryset(cls, actor, mode, location, followed, types):
        queryset = Organization.objects.filter(
            is_active=True,
            type__in=types,
        )

        # Exclude followed orgs, and the actor's own org when acting as one.
        queryset = queryset.exclude(id__in=followed["org_ids"])
        if actor.is_org:
            queryset = queryset.exclude(id=actor.organization.id)

        # A single, condition-scoped join to the primary location. Because
        # is_primary is unique per org (unique_primary_location_per_org), this
        # matches at most one row → no duplicate orgs, whatever the mode.
        queryset = queryset.annotate(
            primary_loc=FilteredRelation(
                "locations",
                condition=Q(locations__is_primary=True),
            )
        )

        if mode == cls.NEARBY:
            lat, lng = location
            box = cls._bounding_box(lat, lng)
            queryset = (
                queryset
                .filter(
                    primary_loc__latitude__gte=box["min_lat"],
                    primary_loc__latitude__lte=box["max_lat"],
                    primary_loc__longitude__gte=box["min_lng"],
                    primary_loc__longitude__lte=box["max_lng"],
                )
                .annotate(
                    distance_km=cls._distance_expr(
                        lat, lng, "primary_loc__latitude", "primary_loc__longitude"
                    ),
                    city=F("primary_loc__city"),
                )
                .order_by("distance_km", "id")
            )
        else:  # POPULAR
            queryset = queryset.annotate(
                followers_count=Coalesce(F("profile__followers_count"), Value(0)),
                city=F("primary_loc__city"),
            ).order_by("-followers_count", "-id")

        return queryset.select_related("profile")

    # ------------------------------------------------------------------ #
    # TRENDING POSTS
    # ------------------------------------------------------------------ #
    @classmethod
    def get_trending_posts_queryset(cls, actor, seen_ids=None):
        """
        Public, recent, high-engagement posts for the Explore feed. Same scoring
        style as FeedService.get_feed_queryset, but engagement-first and tuned
        for discovery:

          final_score = engagement * WEIGHT + recency_boost + followed_penalty

        Works for user and org actors. Own posts, non-public / deleted posts,
        and anything in ``seen_ids`` are filtered out before scoring. Ordered by
        (-final_score, -id) to line up exactly with FeedCursorPagination's
        keyset so paging never duplicates or skips a row.
        """
        followed = FollowService.get_following_ids(actor)
        followed_user_ids = followed["user_ids"]
        followed_org_ids = followed["org_ids"]

        current_time = now()

        # 1. BASE VISIBILITY — public, live, and recent only.
        queryset = Post.objects.filter(
            is_deleted=False,
            visibility=Post.Visibility.PUBLIC,
            created_at__gte=current_time - timedelta(days=cls.TRENDING_WINDOW_DAYS),
        )

        # 2. EXCLUDE the actor's own posts.
        if actor.is_user:
            queryset = queryset.exclude(author_user=actor.user)
        else:
            queryset = queryset.exclude(author_org=actor.organization)

        # 3. VARIETY — drop what the client has already been shown.
        if seen_ids:
            queryset = queryset.exclude(id__in=seen_ids)

        # 4. SCORING
        queryset = queryset.annotate(
            engagement_score=Ln(
                F("likes_count") + F("comments_count") + 1
            ),
            recency_boost=Case(
                When(created_at__gte=current_time - timedelta(hours=6), then=Value(4)),
                When(created_at__gte=current_time - timedelta(hours=24), then=Value(2)),
                When(created_at__gte=current_time - timedelta(hours=72), then=Value(1)),
                default=Value(0),
                output_field=FloatField(),
            ),
            followed_penalty=Case(
                When(
                    author_user_id__in=followed_user_ids,
                    then=Value(cls.TRENDING_FOLLOWED_PENALTY),
                ),
                When(
                    author_org_id__in=followed_org_ids,
                    then=Value(cls.TRENDING_FOLLOWED_PENALTY),
                ),
                default=Value(0),
                output_field=FloatField(),
            ),
        ).annotate(
            final_score=(
                F("engagement_score") * cls.TRENDING_ENGAGEMENT_WEIGHT
                + F("recency_boost")
                + F("followed_penalty")
            )
        ).order_by("-final_score", "-id")

        return queryset.select_related(
            "author_user__profile",
            "author_org__profile",
            "sport",
        ).prefetch_related("media")
