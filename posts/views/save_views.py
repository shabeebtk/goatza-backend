import logging
from django.db import transaction
from rest_framework import status
from rest_framework.exceptions import NotFound

from core.views.base_views import BaseAPIView
from posts.models import Post
from posts.pagination import PostSearchCursorPagination
from posts.serializers.posts_serializers import (
    PostListSerializer, POST_MENTIONS_PREFETCH,
)
from posts.services.saved_post_service import (
    annotate_is_saved, saved_posts_queryset, toggle_save,
)
from feed.services.feed_services import FeedService
from utils.response import response_data

logger = logging.getLogger(__name__)


class ToggleSavePostAPIView(BaseAPIView):
    """
    POST /posts/save   body {"post_id": "<uuid>"}

    Saves for the CURRENT actor — the signed-in person, or the org when acting
    through the X-Actor-* headers. Saves are private to the saver: nothing is
    counted, notified, or shown to the author.
    """

    def post(self, request):
        TAG = "ToggleSavePostAPIView"

        try:
            actor = request.actor
            post_id = request.data.get("post_id")

            if not post_id:
                return response_data(False, "post_id is required", status_code=400)

            if not actor or (not actor.is_user and not actor.is_org):
                return response_data(False, "Invalid actor", status_code=400)

            with transaction.atomic():
                post = Post.objects.filter(id=post_id, is_deleted=False).first()

                if not post:
                    return response_data(False, "Post not found", status_code=404)

                is_saved = toggle_save(actor, post)

            logger.info(
                f"{TAG} | post={post.id} "
                f"| actor={'user:' + str(actor.user.id) if actor.is_user else 'org:' + str(actor.organization.id)} "
                f"| saved={is_saved}"
            )

            return response_data(
                success=True,
                message="Success",
                data={"post_id": str(post.id), "is_saved": is_saved},
            )

        except Exception as e:
            logger.error(f"{TAG} | Error | {str(e)}", exc_info=True)
            return response_data(
                success=False,
                message="Something went wrong",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                error=str(e),
            )


class SavedPostsListAPIView(BaseAPIView):
    """
    GET /posts/saved/list?cursor=<cursor>

    The CURRENT actor's saved posts, most recently saved first. Same item shape
    as the feed so PostCard renders unchanged — including `is_saved`, which is
    trivially true here but must still be present or the bookmark on this very
    list would render empty.
    """

    def get(self, request):
        TAG = "SavedPostsListAPIView"

        try:
            actor = request.actor

            if not actor or (not actor.is_user and not actor.is_org):
                return response_data(False, "Invalid actor", status_code=400)

            # Paginate the SAVE rows, not the posts: the order is "when I saved
            # it", which lives on the save. SavedPost PKs are UUIDv7
            # (time-sortable per CLAUDE.md), so the keyset paginator's "-id"
            # matches the "-created_at" ordering and is backed by the PK index.
            paginator = PostSearchCursorPagination()
            page = paginator.paginate_queryset(
                saved_posts_queryset(actor).order_by("-id"), request
            )
            page_post_ids = [saved.post_id for saved in page]

            posts = annotate_is_saved(
                Post.objects.filter(id__in=page_post_ids), actor
            ).select_related(
                "author_user__profile", "author_org__profile", "sport"
            ).prefetch_related("media", POST_MENTIONS_PREFETCH)

            # `id__in` returns them in whatever order the DB likes — restore the
            # saved-at ordering from the page.
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
                message="Saved posts fetched successfully",
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
