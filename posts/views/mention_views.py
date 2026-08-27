import logging
from rest_framework import status
from rest_framework.exceptions import NotFound
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from core.views.base_views import BaseAPIView
from accounts.models import User
from organization.models import Organization
from posts.models import Post, PostMention
from posts.pagination import PostSearchCursorPagination
from posts.serializers.posts_serializers import (
    PostListSerializer, POST_MENTIONS_PREFETCH,
)
from posts.services.saved_post_service import annotate_is_saved
from feed.services.feed_services import FeedService
from utils.response import response_data
from moderation.selectors.blocked_filters import exclude_blocked

logger = logging.getLogger(__name__)

# Per actor type, so a suggestion list is never one type crowding out the other.
SUGGEST_LIMIT = 5


class MyMentionsAPIView(BaseAPIView):
    """
    GET /posts/mentions/my?cursor=<cursor>

    Posts where the CURRENT ACTOR is mentioned. Scoped by the resolved actor,
    so the same request from the org-admin shell (X-Actor-* headers) returns
    the ORG's mentions and never the signed-in person's — the two lists are
    separate by construction, with no extra wiring on the client.

    Item shape is identical to the feed so PostCard renders unchanged.
    """

    def get(self, request):
        TAG = "MyMentionsAPIView"
        try:
            actor = request.actor

            if not actor or (not actor.is_user and not actor.is_org):
                return response_data(False, "Invalid actor", status_code=400)

            mention_filter = (
                {"mentioned_user": actor.user} if actor.is_user
                else {"mentioned_org": actor.organization}
            )

            # Newest MENTION first, not newest post: an edit that adds you to
            # an old post should surface at the top, which is when it became
            # yours to know about. PostMention PKs are UUIDv7 (time-sortable
            # per CLAUDE.md), so "-id" IS "-created_at" AND is backed by the PK
            # index — which lets the ordinary keyset paginator run directly on
            # the mention rows instead of on the posts.
            mention_queryset = (
                PostMention.objects
                .filter(post__is_deleted=False, **mention_filter)
                .order_by("-id")
            )

            paginator = PostSearchCursorPagination()
            page = paginator.paginate_queryset(mention_queryset, request)
            page_post_ids = [mention.post_id for mention in page]

            posts = annotate_is_saved(
                Post.objects.filter(id__in=page_post_ids), actor
            ).select_related(
                "author_user__profile", "author_org__profile", "sport"
            ).prefetch_related("media", POST_MENTIONS_PREFETCH)

            # `id__in` returns them in whatever order the DB likes — restore
            # the mention ordering from the page.
            by_id = {post.id: post for post in posts}
            ordered = [by_id[pid] for pid in page_post_ids if pid in by_id]

            user_reactions = FeedService.get_actor_reactions(
                actor, [post.id for post in ordered]
            )

            serializer = PostListSerializer(
                ordered,
                many=True,
                context={"user_reactions": user_reactions},
            )

            return response_data(
                success=True,
                message="Mentions fetched successfully",
                data={
                    "next_cursor": paginator.get_next_cursor(),
                    "results": serializer.data,
                },
            )

        except NotFound as e:
            # Malformed pagination cursor — same contract as /posts/search.
            return response_data(False, message=str(e.detail), status_code=400)

        except Exception as e:
            logger.error(f"{TAG} | Error | {str(e)}")
            return response_data(
                success=False,
                message="Something went wrong",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                error=str(e),
            )


class MentionSuggestAPIView(BaseAPIView):
    """
    GET /posts/mention/suggest?q=<term>

    Composer autocomplete. Prefix-only (`istartswith`) against the indexed
    username columns on both models — this runs on every keystroke after the
    client's debounce, so it must stay an index seek, never a scan.

    WHICH hat you wear does not change the ranking — but it does change the
    block set, so this is a BaseAPIView (it was a bare APIView while the list
    was actor-independent): an org actor and its owner can have different
    people blocked, and a handle the write path would silently drop must not
    be offered here.
    """

    def get(self, request):
        TAG = "MentionSuggestAPIView"
        try:
            q = (request.query_params.get("q") or "").strip().lstrip("@")

            # Below one character every handle matches — not a suggestion.
            if not q:
                return response_data(
                    success=True,
                    message="Suggestions fetched successfully",
                    data={"users": [], "organizations": []},
                )

            # BLOCK EXCLUSION — suggesting a blocked handle would be a
            # dead end: sync_post_mentions silently drops the mention anyway,
            # so the picker must not offer it in the first place.
            actor = request.actor

            users = (
                exclude_blocked(
                    User.objects.filter(username__istartswith=q, is_active=True),
                    actor,
                    user_field="id",
                    org_field=None,
                )
                .select_related("profile")
                .order_by("username")[:SUGGEST_LIMIT]
            )

            organizations = (
                exclude_blocked(
                    Organization.objects.filter(
                        username__istartswith=q, is_active=True
                    ),
                    actor,
                    user_field=None,
                    org_field="id",
                )
                .select_related("profile")
                .order_by("username")[:SUGGEST_LIMIT]
            )

            return response_data(
                success=True,
                message="Suggestions fetched successfully",
                data={
                    "users": [
                        {
                            "id": str(user.id),
                            "username": user.username or "",
                            "name": getattr(user.profile, "name", "") or "",
                            "profile_photo": getattr(
                                user.profile, "profile_photo", ""
                            ) or "",
                        }
                        for user in users
                    ],
                    "organizations": [
                        {
                            "id": str(org.id),
                            "username": org.username or "",
                            "name": org.name or "",
                            # OrganizationProfile calls it `logo`, not
                            # `profile_photo` — the client maps it to the avatar.
                            "logo": getattr(org.profile, "logo", "") or "",
                        }
                        for org in organizations
                    ],
                },
            )

        except Exception as e:
            logger.error(f"{TAG} | Error | {str(e)}")
            return response_data(
                success=False,
                message="Something went wrong",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                error=str(e),
            )
