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

logger = logging.getLogger(__name__)

class CreateCommentAPIView(BaseAPIView):

    def post(self, request):
        TAG = "CreateCommentAPIView"

        try:
            actor = request.actor
            post_id = request.data.get("post_id")
            text = (request.data.get("comment") or "").strip()
            parent_id = request.data.get("parent_id")

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

                parent = None
                root = None

                if parent_id:
                    parent = Comment.objects.select_for_update().filter(
                        id=parent_id,
                        post_id=post.id,
                        is_deleted=False
                    ).only("id", "parent", "reply_count").first()

                    if not parent:
                        return response_data(False, "Invalid parent comment", status_code=400)

                    # CORE LOGIC (FLAT THREAD)
                    root = parent.parent if parent.parent else parent

                comment_data = {
                    "post": post,
                    "comment": text,
                    "parent": root,
                    "reply_to": parent if parent else None
                }

                if actor.is_user:
                    comment_data["user"] = actor.user
                else:
                    comment_data["organization"] = actor.organization

                comment = Comment.objects.create(**comment_data)

                # increment post count
                Post.objects.filter(id=post.id).update(
                    comments_count=F("comments_count") + 1
                )

                # increment reply count ONLY on root
                if root:
                    Comment.objects.filter(id=root.id).update(
                        reply_count=F("reply_count") + 1
                    )

            return response_data(
                success=True,
                message="Comment added",
                data={"comment_id": str(comment.id)}
            )

        except Exception as e:
            logger.error(f"{TAG} | Error | {str(e)}")
            return response_data(False, "Something went wrong", status_code=500, error=str(e))


class ListCommentsAPIView(BaseAPIView):

    def get(self, request):
        TAG = "ListCommentsAPIView"

        try:
            post_id = request.query_params.get("post_id")

            limit = min(int(request.query_params.get("limit", 20)), 50)
            offset = max(int(request.query_params.get("offset", 0)), 0)

            if not post_id:
                return response_data(False, "post_id is required", status_code=400)

            # BASE QUERY
            queryset = Comment.objects.filter(
                post_id=post_id,
                parent__isnull=True,
                is_deleted=False
            ).select_related(
                "user__profile",
                "organization"
            ).order_by("-created_at")[offset: offset + limit]

            # PREFETCH REPLIES 
            replies_qs = Comment.objects.filter(
                is_deleted=False
            ).select_related(
                "user__profile",
                "organization"
            ).order_by("created_at")

            queryset = queryset.prefetch_related(
                Prefetch("replies", queryset=replies_qs, to_attr="all_replies")
            )

            total_count = Comment.objects.filter(
                post_id=post_id,
                parent__isnull=True,
                is_deleted=False
            ).count()

            serializer = CommentSerializer(queryset, many=True)

            return response_data(
                success=True,
                data={
                    "count": total_count,
                    "limit": limit,
                    "offset": offset,
                    "results": serializer.data
                }
            )

        except Exception as e:
            logger.error(f"{TAG} | Error | {str(e)}")

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

            limit = int(request.query_params.get("limit", 20))
            offset = int(request.query_params.get("offset", 0))

            if not parent_id:
                return response_data(False, "parent_id is required", status_code=400)

            queryset = Comment.objects.filter(
                parent_id=parent_id,
                is_deleted=False
            ).select_related(
                "user__profile",
                "organization"
            ).order_by("created_at")[offset: offset + limit]

            serializer = CommentSerializer(queryset, many=True)

            return response_data(
                success=True,
                data={"results": serializer.data}
            )

        except Exception as e:
            logger.error(f"{TAG} | Error | {str(e)}")

            return response_data(
                success=False,
                message="Something went wrong",
                status_code=500,
                error=str(e)
            )