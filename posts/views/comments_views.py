import logging
from django.db import transaction
from django.db.models import Q, F
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

            # -------------------------
            # 🔒 VALIDATION
            # -------------------------
            if not post_id:
                return response_data(False, "post_id is required", status_code=400)

            if not text:
                return response_data(False, "comment is required", status_code=400)

            post = Post.objects.filter(id=post_id, is_deleted=False).only("id").first()

            if not post:
                return response_data(False, "Post not found", status_code=404)

            parent = None
            if parent_id:
                parent = Comment.objects.filter(
                    id=parent_id,
                    post_id=post.id,
                    is_deleted=False
                ).only("id").first()

                if not parent:
                    return response_data(False, "Invalid parent comment", status_code=400)

            # -------------------------
            # CREATE COMMENT
            # -------------------------
            with transaction.atomic():

                comment_data = {
                    "post": post,
                    "comment": text,
                    "parent": parent
                }

                if actor.is_user:
                    comment_data["user"] = actor.user
                else:
                    comment_data["organization"] = actor.organization

                comment = Comment.objects.create(**comment_data)

                # increment counter
                Post.objects.filter(id=post.id).update(
                    comments_count=F("comments_count") + 1
                )

            logger.info(f"{TAG} | post={post.id} | comment_id={comment.id}")

            return response_data(
                success=True,
                message="Comment added",
                data={"comment_id": str(comment.id)}
            )

        except Exception as e:
            logger.error(f"{TAG} | Error | {str(e)}")

            return response_data(
                success=False,
                message="Something went wrong",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                error=str(e)
            )


class ListCommentsAPIView(BaseAPIView):

    def get(self, request):
        TAG = "ListCommentsAPIView"

        try:
            post_id = request.query_params.get("post_id")

            limit = int(request.query_params.get("limit", 20))
            offset = int(request.query_params.get("offset", 0))

            limit = min(limit, 50)
            offset = max(offset, 0)

            if not post_id:
                return response_data(False, "post_id is required", status_code=400)

            queryset = Comment.objects.filter(
                post_id=post_id,
                parent__isnull=True,
                is_deleted=False
            )

            total_count = queryset.count()

            queryset = queryset.select_related(
                "user__profile",
                "organization"
            ).order_by("-created_at")[offset: offset + limit]

            serializer = CommentSerializer(queryset, many=True)

            logger.info(f"{TAG} | count={len(serializer.data)}")

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