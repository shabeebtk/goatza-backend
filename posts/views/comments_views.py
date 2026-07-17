import logging
from django.db import transaction
from django.db.models import F, Prefetch, Value
from django.db.models.functions import Greatest
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


class DeleteCommentAPIView(BaseAPIView):
    '''
    Soft-delete a comment.

    DELETE /posts/comments/delete?comment_id=<uuid>

    Allowed for: the comment's author OR the post's owner (active actor).
    Deleting a top-level comment cascades to its replies. Denormalized counters
    (post.comments_count, root.reply_count) are decremented accordingly.
    '''
    def delete(self, request):
        TAG = "DeleteCommentAPIView"

        try:
            actor = request.actor
            comment_id = request.query_params.get("comment_id")

            if not actor or (not actor.is_user and not actor.is_org):
                return response_data(False, "Invalid actor", status_code=400)

            if not comment_id:
                return response_data(False, "comment_id is required", status_code=400)

            with transaction.atomic():
                comment = (
                    Comment.objects
                    .select_for_update()
                    .select_related("post")
                    .filter(id=comment_id, is_deleted=False)
                    .first()
                )

                if not comment:
                    return response_data(False, "Comment not found", status_code=404)

                post = comment.post

                # -------------------------
                # PERMISSION — comment author OR post owner
                # -------------------------
                if actor.is_user:
                    is_author = comment.user_id == actor.user.id
                    is_post_owner = post.author_user_id == actor.user.id
                else:
                    is_author = comment.organization_id == actor.organization.id
                    is_post_owner = post.author_org_id == actor.organization.id

                if not (is_author or is_post_owner):
                    return response_data(False, "Not allowed to delete this comment", status_code=403)

                # -------------------------
                # SOFT DELETE (+ cascade for a top-level comment)
                # -------------------------
                if comment.parent_id is None:
                    reply_ids = list(
                        Comment.objects.filter(parent_id=comment.id, is_deleted=False)
                        .values_list("id", flat=True)
                    )
                    removed = 1 + len(reply_ids)

                    if reply_ids:
                        Comment.objects.filter(id__in=reply_ids).update(is_deleted=True)

                    comment.is_deleted = True
                    comment.save(update_fields=["is_deleted"])
                else:
                    removed = 1
                    comment.is_deleted = True
                    comment.save(update_fields=["is_deleted"])

                    # Drop the reply from its root's denormalized count
                    Comment.objects.filter(id=comment.parent_id).update(
                        reply_count=Greatest(F("reply_count") - 1, Value(0))
                    )

                # Decrement the post's total comment count (never below zero)
                Post.objects.filter(id=post.id).update(
                    comments_count=Greatest(F("comments_count") - removed, Value(0))
                )

            logger.info(f"{TAG} | comment={comment_id} | removed={removed}")

            return response_data(True, message="Comment deleted")

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