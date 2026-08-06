"""
The anonymous-reachable profile endpoints, mounted under /public/.

Every view here extends ``PublicAPIView``: AllowAny + an IP-keyed throttle, with
``ActorMixin`` still running so a signed-in caller is resolved as their normal
actor and gets relationship data and their own followers-only content. No
existing endpoint's permissions change — this is a parallel surface.

Two design points worth stating:

  * The bundle. The profile page is server-rendered, and a page that needed six
    round trips before it could paint would defeat the point of making it
    shareable. One GET returns the header, sports, career, achievements,
    highlights and the first page of posts.

  * 404, never 403. A hidden profile, a deactivated user, a user with no
    username and an outright typo are indistinguishable in the response. A 403
    would confirm the account exists, which is exactly what someone probing a
    hidden profile wants.
"""

import logging

from accounts.serializers.public_profile_serializers import (
    PublicUserProfileSerializer,
)
from achievements.serializers.achievement_serializers import (
    AchievementSerializer,
)
from careers.serializers.career_serializers import CareerEntrySerializer
from core.constant import TYPE_ORGANIZATION, TYPE_USER
from core.selectors.public_profile_selectors import (
    PUBLIC_POSTS_PAGE_SIZE,
    clamp_page,
    get_public_organization,
    get_public_user,
    org_profile_ref,
    public_achievements_for,
    public_career_entries_for,
    public_highlights_for,
    public_posts_page,
    public_recruitments_for,
    user_profile_ref,
)
from core.views.base_views import PublicAPIView
from highlights.serializers.highlight_serializers import HighlightSerializer
from organization.serializers.public_profile_serializers import (
    PublicOrganizationProfileSerializer,
)
from posts.serializers.posts_serializers import PostListSerializer
from recruitments.serializers.recruitment_list_serializers import (
    RecruitmentListSerializer,
)
from utils.cache import cache_get, cache_set
from utils.cache_keys import CacheKeys
from utils.response import response_data

logger = logging.getLogger(__name__)

# A shared profile can go viral in minutes. 60s is short enough that a rename or
# a new post shows up almost immediately and long enough that a link doing the
# rounds in a group chat costs one query set, not one per tap.
PUBLIC_BUNDLE_TTL = 60


def _not_found(what):
    return response_data(
        success=False,
        message=f"{what} not found",
        status_code=404,
    )


def _serialize_posts(queryset, total, limit, offset):
    """
    The paginated shape both the bundle and the posts endpoint return.

    ``user_reactions`` is deliberately empty: an anonymous viewer has reacted to
    nothing, and a signed-in one landing here still gets a correct "not
    reacted" — this surface is the shareable read view, not the in-app feed.
    """
    return {
        "count": total,
        "limit": limit,
        "offset": offset,
        "results": PostListSerializer(
            queryset, many=True, context={"user_reactions": {}}
        ).data,
    }


# ─────────────────────────────────────────────
# USER
# ─────────────────────────────────────────────

class PublicUserProfileAPIView(PublicAPIView):
    """
    GET /public/profile/<username>

    Bundle: profile + sports + career + achievements + highlights + first page
    of posts.
    """

    def get(self, request, username):
        TAG = "PublicUserProfileAPIView"

        try:
            actor = request.actor

            # Only the anonymous rendering is cacheable. A signed-in caller's
            # bundle depends on who they are (their own followers-only posts,
            # their full highlights rail), so it must never be written to — or
            # read from — a key shared with everybody.
            cache_key = CacheKeys.public_user_profile(username)
            if actor is None:
                cached = cache_get(cache_key)
                if cached is not None:
                    return response_data(success=True, data=cached)

            user = get_public_user(username)
            if user is None:
                return _not_found("Profile")

            posts, total = public_posts_page(
                user_profile_ref(user), actor,
                limit=PUBLIC_POSTS_PAGE_SIZE, offset=0,
            )

            data = {
                "type": TYPE_USER,
                "profile": PublicUserProfileSerializer(user).data,
                "highlights": HighlightSerializer(
                    public_highlights_for(user, actor),
                    many=True,
                    # Never the owner view on this surface: `visibility` and
                    # `views_count` are the owner's business and the owner reads
                    # their own rail through the authenticated endpoint.
                    context={"is_owner": False},
                ).data,
                "career": CareerEntrySerializer(
                    public_career_entries_for(user), many=True
                ).data,
                "achievements": AchievementSerializer(
                    public_achievements_for(user), many=True
                ).data,
                "posts": _serialize_posts(
                    posts, total, PUBLIC_POSTS_PAGE_SIZE, 0
                ),
            }

            if actor is None:
                cache_set(cache_key, data, timeout=PUBLIC_BUNDLE_TTL)

            return response_data(success=True, data=data)

        except Exception as e:
            logger.error(f"{TAG} | Error | username={username} | {str(e)}")
            return response_data(
                success=False,
                message="Something went wrong",
                status_code=500,
            )


class PublicUserPostsAPIView(PublicAPIView):
    """GET /public/profile/<username>/posts?limit&offset — pagination only."""

    def get(self, request, username):
        TAG = "PublicUserPostsAPIView"

        try:
            user = get_public_user(username)
            if user is None:
                return _not_found("Profile")

            limit, offset = clamp_page(
                request.query_params.get("limit"),
                request.query_params.get("offset"),
            )

            posts, total = public_posts_page(
                user_profile_ref(user), request.actor,
                limit=limit, offset=offset,
            )

            return response_data(
                success=True,
                data=_serialize_posts(posts, total, limit, offset),
            )

        except Exception as e:
            logger.error(f"{TAG} | Error | username={username} | {str(e)}")
            return response_data(
                success=False,
                message="Something went wrong",
                status_code=500,
            )


# ─────────────────────────────────────────────
# ORGANIZATION
# ─────────────────────────────────────────────

class PublicOrganizationProfileAPIView(PublicAPIView):
    """
    GET /public/organization/<username>

    Bundle: profile + sports + locations + active public recruitments + first
    page of posts.
    """

    def get(self, request, username):
        TAG = "PublicOrganizationProfileAPIView"

        try:
            actor = request.actor

            cache_key = CacheKeys.public_org_profile(username)
            if actor is None:
                cached = cache_get(cache_key)
                if cached is not None:
                    return response_data(success=True, data=cached)

            organization = get_public_organization(username)
            if organization is None:
                return _not_found("Organization")

            posts, total = public_posts_page(
                org_profile_ref(organization), actor,
                limit=PUBLIC_POSTS_PAGE_SIZE, offset=0,
            )

            data = {
                "type": TYPE_ORGANIZATION,
                "profile": PublicOrganizationProfileSerializer(
                    organization
                ).data,
                "recruitments": RecruitmentListSerializer(
                    public_recruitments_for(organization), many=True
                ).data,
                "posts": _serialize_posts(
                    posts, total, PUBLIC_POSTS_PAGE_SIZE, 0
                ),
            }

            if actor is None:
                cache_set(cache_key, data, timeout=PUBLIC_BUNDLE_TTL)

            return response_data(success=True, data=data)

        except Exception as e:
            logger.error(f"{TAG} | Error | username={username} | {str(e)}")
            return response_data(
                success=False,
                message="Something went wrong",
                status_code=500,
            )


class PublicOrganizationPostsAPIView(PublicAPIView):
    """GET /public/organization/<username>/posts?limit&offset."""

    def get(self, request, username):
        TAG = "PublicOrganizationPostsAPIView"

        try:
            organization = get_public_organization(username)
            if organization is None:
                return _not_found("Organization")

            limit, offset = clamp_page(
                request.query_params.get("limit"),
                request.query_params.get("offset"),
            )

            posts, total = public_posts_page(
                org_profile_ref(organization), request.actor,
                limit=limit, offset=offset,
            )

            return response_data(
                success=True,
                data=_serialize_posts(posts, total, limit, offset),
            )

        except Exception as e:
            logger.error(f"{TAG} | Error | username={username} | {str(e)}")
            return response_data(
                success=False,
                message="Something went wrong",
                status_code=500,
            )
