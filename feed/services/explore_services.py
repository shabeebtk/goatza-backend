# feed/services/explore_services.py

from collections import defaultdict, deque
from datetime import timedelta

from django.db.models import (
    Case, When, F, Q, Value, FloatField, FilteredRelation,
)
from django.db.models.functions import Coalesce, Ln
from django.utils.timezone import now

from services.geo import haversine

from accounts.models import User
from posts.serializers.posts_serializers import POST_MENTIONS_PREFETCH
from organization.models import Organization, OrganizationLocation
from posts.models import Post
from connections.services.follow_services import FollowService
from feed.pagination import ExploreCursorPagination
from moderation.selectors.blocked_filters import exclude_blocked


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

    # Re-exported from services.geo.haversine, which owns the trig now that
    # recruitment discovery ranks by distance too (see _distance_expr).
    EARTH_RADIUS_KM = haversine.EARTH_RADIUS_KM
    KM_PER_DEG_LAT = haversine.KM_PER_DEG_LAT

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
    def _bounding_box(cls, lat, lng, radius):
        """Delegates to services.geo.haversine — see that module for why."""
        return haversine.bounding_box(lat, lng, radius)

    @classmethod
    def _distance_expr(cls, lat, lng, lat_field, lng_field):
        """Delegates to services.geo.haversine — see that module for why."""
        return haversine.distance_expr(lat, lng, lat_field, lng_field)

    # ------------------------------------------------------------------ #
    # FILTER HELPERS
    # ------------------------------------------------------------------ #
    @staticmethod
    def _has_any_filter(filters):
        """
        True when a search / sport / position / location filter is active.
        Drives two behaviours: forcing the mode (see ``_discover``) and letting
        followed accounts back into the results.
        """
        return bool(
            filters.get("search")
            or filters.get("sport_id")
            or filters.get("position_id")
            or filters.get("lat") is not None
        )

    # ------------------------------------------------------------------ #
    # SHARED ORCHESTRATION
    # ------------------------------------------------------------------ #
    @classmethod
    def _discover(cls, actor, request, build_queryset, filters):
        """
        Shared paginate-with-mode flow for both endpoints.

        ``build_queryset(mode, center, radius)`` returns the queryset for a mode.

        Mode selection (first page):
          1. A location filter (lat/lng) forces "nearby" centered on the FILTER
             coords with ``radius_km`` — and there is NO popular fallback (an
             empty radius result is correct, not a reason to ignore the filter).
          2. A "search" with no location filter forces "popular" (global) — a
             name search must not be geo-restricted to the actor's radius.
          3. Otherwise the original behaviour: actor-location nearby with a
             one-time popular fallback when the first page is empty.

        With a cursor the mode is locked to whatever the cursor carries (the
        client resets the cursor when filters change), so no re-decision happens.
        """
        cursor = request.query_params.get("cursor")

        filter_lat = filters.get("lat")
        filter_lng = filters.get("lng")
        has_location_filter = filter_lat is not None and filter_lng is not None
        has_search = bool(filters.get("search"))
        # Explicit "nearby" | "popular" a rail asked for (see parse_explore_filters).
        forced_mode = filters.get("mode")

        # Center precedence: filter coords > actor location.
        if has_location_filter:
            center = (filter_lat, filter_lng)
            radius = filters.get("radius_km") or cls.NEARBY_RADIUS_KM
        else:
            center = cls.resolve_location(actor)
            radius = cls.NEARBY_RADIUS_KM

        paginator = ExploreCursorPagination()

        if cursor:
            # Raises NotFound on a malformed cursor (handled by the view).
            mode = paginator.decode_mode(cursor)
        elif forced_mode:
            # A rail explicitly asked for one mode — honor it verbatim (no auto
            # pick, no popular fallback) so popular + nearby can be shown side by
            # side as separate rails.
            mode = forced_mode
        elif has_location_filter:
            mode = cls.NEARBY
        elif has_search:
            mode = cls.POPULAR
        else:
            mode = cls.NEARBY if center else cls.POPULAR

        # Nearby needs a usable center. Without one, an EXPLICIT nearby request
        # returns nothing (its rail self-hides → "no location, no nearby rail"),
        # while the auto pick degrades to popular instead of erroring.
        if mode == cls.NEARBY and center is None:
            if forced_mode == cls.NEARBY:
                return {"results": [], "mode": cls.NEARBY, "next_cursor": None}
            mode = cls.POPULAR

        queryset = build_queryset(mode, center, radius)
        page = paginator.paginate_queryset(queryset, request, mode)

        # One-time popular fallback ONLY for the plain actor-nearby case: never
        # when a location filter is active (empty is the correct answer), and
        # never for an explicit rail mode (an empty forced-nearby rail must stay
        # empty, not silently show popular results under a "nearby" heading).
        allow_fallback = (
            not cursor
            and mode == cls.NEARBY
            and not has_location_filter
            and not forced_mode
        )
        if allow_fallback and not page:
            mode = cls.POPULAR
            queryset = build_queryset(mode, center, radius)
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
    def discover_players(cls, actor, request, filters=None):
        filters = filters or {}
        followed = FollowService.get_following_ids(actor)

        def build_queryset(mode, center, radius):
            return cls._players_queryset(actor, mode, center, radius, followed, filters)

        result = cls._discover(actor, request, build_queryset, filters)
        # Hand the followed id sets to the view for the ``is_following`` field —
        # no extra query, reuses the sets we already resolved above.
        result["followed_user_ids"] = set(followed["user_ids"])
        result["followed_org_ids"] = set(followed["org_ids"])
        return result

    @classmethod
    def _players_queryset(cls, actor, mode, center, radius, followed, filters):
        # Base pool: players that actually have a profile.
        queryset = User.objects.filter(
            role=User.Role.PLAYER,
            profile__isnull=False,
        )

        # Always hide the actor's own account.
        if actor.is_user:
            queryset = queryset.exclude(id=actor.user.id)

        # BLOCK EXCLUSION — rows here ARE the identities, so the blocked
        # user ids match on "id" and there is no org column to match.
        queryset = exclude_blocked(queryset, actor, user_field="id", org_field=None)

        # Discovery semantics: exclude followed ONLY when browsing the raw rails.
        # As soon as any filter/search is active, followed accounts are allowed
        # back in (searching a name you follow must find them).
        if not cls._has_any_filter(filters):
            queryset = queryset.exclude(id__in=followed["user_ids"])

        # search / sport / position filters
        search = filters.get("search")
        if search:
            queryset = queryset.filter(
                Q(profile__name__icontains=search) | Q(username__icontains=search)
            )
        sport_id = filters.get("sport_id")
        if sport_id:
            # Unique (user, sport) → no row multiplication, no distinct() needed.
            queryset = queryset.filter(sports__sport_id=sport_id)
        position_id = filters.get("position_id")
        if position_id:
            # Only reachable with sport_id (validated in the view). Unique
            # (user, position) → still no duplicates.
            queryset = queryset.filter(
                positions__sport_id=sport_id,
                positions__position_id=position_id,
            )

        if mode == cls.NEARBY:
            lat, lng = center
            box = cls._bounding_box(lat, lng, radius)
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
            )
            # An explicit location filter wants the exact circle, not just the
            # square prefilter. The rails (no location filter) keep their
            # box-only behaviour unchanged.
            if filters.get("lat") is not None:
                queryset = queryset.filter(distance_km__lte=radius)
            queryset = queryset.order_by("distance_km", "id")
        else:  # POPULAR
            queryset = queryset.annotate(
                followers_count=Coalesce(F("profile__followers_count"), Value(0)),
            ).order_by("-followers_count", "-id")

        return queryset.select_related("profile")

    # ------------------------------------------------------------------ #
    # ORGANIZATIONS
    # ------------------------------------------------------------------ #
    @classmethod
    def discover_organizations(cls, actor, request, types, filters=None):
        filters = filters or {}
        followed = FollowService.get_following_ids(actor)

        def build_queryset(mode, center, radius):
            return cls._orgs_queryset(actor, mode, center, radius, followed, types, filters)

        result = cls._discover(actor, request, build_queryset, filters)
        result["followed_user_ids"] = set(followed["user_ids"])
        result["followed_org_ids"] = set(followed["org_ids"])
        return result

    @classmethod
    def _orgs_queryset(cls, actor, mode, center, radius, followed, types, filters):
        queryset = Organization.objects.filter(
            is_active=True,
            type__in=types,
        )

        # Always hide the actor's own org.
        if actor.is_org:
            queryset = queryset.exclude(id=actor.organization.id)

        # BLOCK EXCLUSION — same shape as players, org side.
        queryset = exclude_blocked(queryset, actor, user_field=None, org_field="id")

        # Exclude followed ONLY when browsing the raw rails (see players).
        if not cls._has_any_filter(filters):
            queryset = queryset.exclude(id__in=followed["org_ids"])

        # search / sport filters
        search = filters.get("search")
        if search:
            queryset = queryset.filter(
                Q(name__icontains=search) | Q(username__icontains=search)
            )
        sport_id = filters.get("sport_id")
        if sport_id:
            # Unique (organization, sport) → no duplicates, no distinct() needed.
            queryset = queryset.filter(sports__sport_id=sport_id)

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
            lat, lng = center
            box = cls._bounding_box(lat, lng, radius)
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
            )
            if filters.get("lat") is not None:
                queryset = queryset.filter(distance_km__lte=radius)
            queryset = queryset.order_by("distance_km", "id")
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

        # BLOCK EXCLUSION — before scoring, so a blocked author's post cannot
        # occupy a slot in the diversified page.
        queryset = exclude_blocked(queryset, actor)

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
        ).prefetch_related("media", POST_MENTIONS_PREFETCH)

    # ------------------------------------------------------------------ #
    # DISPLAY DIVERSIFICATION
    # ------------------------------------------------------------------ #
    @staticmethod
    def diversify_posts(posts):
        """
        Round-robin one page's posts so a single author does not open the page
        with a block of their own content.

        This used to live on FeedService and serve both feeds. The home feed no
        longer needs it: it applies a real per-author cap across the whole
        candidate window before paging (§3.3), which this cannot do — running
        after pagination, over 15 already-chosen rows, it can only reorder what
        one author already monopolized. Explore still pages on a keyset over its
        own score, so for it this remains the right (display-only) tool.
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
