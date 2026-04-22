import logging
from django.db import transaction
from django.db.models import F, Prefetch
from rest_framework import status
from rest_framework.exceptions import ValidationError

from core.views.base_views import BaseAPIView
from posts.models import Post, PostMedia, Like, Comment
from sports.models import Sport
from utils.response import response_data
from connections.models import Follow
from posts.serializers.comments_serializers import CommentSerializer
from notifications.services.notification_service import NotificationService

logger = logging.getLogger(__name__)



class CreateCommentAPIView(BaseAPIView):

    def post(self, request):
        TAG = "CreateCommentAPIView"

        try:
            actor = request.actor
            post_id = request.data.get("post_id")
            text = (request.data.get("comment") or "").strip()
            parent_id = request.data.get("parent_id")

            if not actor or (not actor.is_user and not actor.is_org):
                return response_data(False, "Invalid actor", status_code=400)

            if not post_id:
                return response_data(False, "post_id is required", status_code=400)

            if not text:
                return response_data(False, "comment is required", status_code=400)

            with transaction.atomic():

                post = Post.objects.select_for_update().filter(
                    id=post_id,
                    is_deleted=False
                ).only("id", "comments_count").first()

                if not post:
                    return response_data(False, "Post not found", status_code=404)

                root = None

                if parent_id:
                    parent = Comment.objects.select_for_update().filter(
                        id=parent_id,
                        post_id=post.id,
                        is_deleted=False
                    ).only("id", "parent_id", "reply_count").first()

                    if not parent:
                        return response_data(False, "Invalid parent comment", status_code=400)

                    # FLAT THREAD: always attach to the top-level root
                    root = parent if parent.parent_id is None else Comment(id=parent.parent_id)

                comment_data = {
                    "post": post,
                    "comment": text,
                    "parent": root,
                    # reply_to tracks who in the thread this directly targets
                    "reply_to_id": parent_id if parent_id else None,
                }

                if actor.is_user:
                    comment_data["user"] = actor.user
                else:
                    comment_data["organization"] = actor.organization

                comment = Comment.objects.create(**comment_data)

                # Increment post comment count
                Post.objects.filter(id=post.id).update(
                    comments_count=F("comments_count") + 1
                )

                # Increment reply count on root only
                if root:
                    Comment.objects.filter(id=root.id).update(
                        reply_count=F("reply_count") + 1
                    )

                NotificationService.comment(
                    actor_user=actor.user if actor.is_user else None,
                    actor_org=actor.organization if actor.is_org else None,
                    comment=comment
                )

            logger.info(
                f"{TAG} | post={post_id} | comment={comment.id} "
                f"| actor={'user:' + str(actor.user.id) if actor.is_user else 'org:' + str(actor.organization.id)}"
            )

            return response_data(
                success=True,
                message="Comment added",
                data={"comment_id": str(comment.id)}
            )

        except Exception as e:
            logger.error(f"{TAG} | Error | {str(e)}", exc_info=True)
            return response_data(False, "Something went wrong", status_code=500, error=str(e))


class ListCommentsAPIView(BaseAPIView):

    def get(self, request):
        TAG = "ListCommentsAPIView"

        try:
            post_id = request.query_params.get("post_id")

            try:
                limit = min(int(request.query_params.get("limit", 20)), 50)
                offset = max(int(request.query_params.get("offset", 0)), 0)
            except (ValueError, TypeError):
                return response_data(False, "Invalid pagination params", status_code=400)

            if not post_id:
                return response_data(False, "post_id is required", status_code=400)

            if not Post.objects.filter(id=post_id, is_deleted=False).exists():
                return response_data(False, "Post not found", status_code=404)

            # COUNT top-level comments only (cheap, no joins)
            total_count = Comment.objects.filter(
                post_id=post_id,
                parent__isnull=True,
                is_deleted=False
            ).count()

            # Replies prefetch — select_related covers both actor types
            replies_qs = Comment.objects.filter(
                is_deleted=False
            ).select_related(
                "user__profile",
                "organization",
                # reply_to actor fields needed by CommentReplySerializer
                "reply_to__user__profile",
                "reply_to__organization",
            ).order_by("created_at")

            # Main queryset — paginate then prefetch
            queryset = Comment.objects.filter(
                post_id=post_id,
                parent__isnull=True,
                is_deleted=False
            ).select_related(
                "user__profile",
                "organization",
            ).prefetch_related(
                Prefetch("replies", queryset=replies_qs, to_attr="all_replies")
            ).order_by("-created_at")[offset: offset + limit]

            serializer = CommentSerializer(queryset, many=True)

            logger.info(f"{TAG} | post={post_id} | total={total_count} | returned={len(queryset)}")

            return response_data(
                success=True,
                data={
                    "count": total_count,
                    "limit": limit,
                    "offset": offset,
                    "results": serializer.data,
                }
            )

        except Exception as e:
            logger.error(f"{TAG} | Error | {str(e)}", exc_info=True)
            return response_data(
                success=False,
                message="Something went wrong",
                status_code=500,
                error=str(e)
            )


class ListRepliesAPIView(BaseAPIView):

    def get(self, request):
        TAG = "ListRepliesAPIView"

        try:
            parent_id = request.query_params.get("parent_id")

            try:
                limit = min(int(request.query_params.get("limit", 20)), 50)
                offset = max(int(request.query_params.get("offset", 0)), 0)
            except (ValueError, TypeError):
                return response_data(False, "Invalid pagination params", status_code=400)

            if not parent_id:
                return response_data(False, "parent_id is required", status_code=400)

            # Verify the parent comment is a root (not itself a reply)
            parent_exists = Comment.objects.filter(
                id=parent_id,
                parent__isnull=True,   # only root comments can be parent_id here
                is_deleted=False
            ).exists()

            if not parent_exists:
                return response_data(False, "Parent comment not found", status_code=404)

            queryset = Comment.objects.filter(
                parent_id=parent_id,
                is_deleted=False
            ).select_related(
                "user__profile",
                "organization",
                # reply_to actor fields for CommentReplySerializer
                "reply_to__user__profile",
                "reply_to__organization",
            ).order_by("created_at")[offset: offset + limit]

            serializer = CommentSerializer(queryset, many=True)

            return response_data(
                success=True,
                data={
                    "limit": limit,
                    "offset": offset,
                    "results": serializer.data,
                }
            )

        except Exception as e:
            logger.error(f"{TAG} | Error | {str(e)}", exc_info=True)
            return response_data(
                success=False,
                message="Something went wrong",
                status_code=500,
                error=str(e)
            )