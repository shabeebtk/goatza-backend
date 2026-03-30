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
from posts.serializers.like_serializers import LikeListSerializer

logger = logging.getLogger(__name__)


class ToggleLikeAPIView(BaseAPIView):

    def post(self, request):
        TAG = "ToggleLikeAPIView"

        try:
            actor = request.actor
            post_id = request.data.get("post_id")

            # -------------------------
            # VALIDATION
            # -------------------------
            if not post_id:
                return response_data(False, "post_id is required", status_code=400)

            post = Post.objects.filter(
                id=post_id,
                is_deleted=False
            ).only("id", "likes_count").first()

            if not post:
                return response_data(False, "Post not found", status_code=404)

            # -------------------------
            # BUILD FILTERS
            # -------------------------
            like_filter = {"post_id": post.id}

            if actor.is_user:
                like_filter["user"] = actor.user
            else:
                like_filter["organization"] = actor.organization

            # -------------------------
            # TOGGLE
            # -------------------------
            with transaction.atomic():

                existing_like = Like.objects.filter(**like_filter).first()

                if existing_like:
                    # ❌ UNLIKE
                    existing_like.delete()

                    Post.objects.filter(id=post.id).update(
                        likes_count=F("likes_count") - 1
                    )

                    is_liked = False

                else:
                    # ✅ LIKE
                    Like.objects.create(**like_filter)

                    Post.objects.filter(id=post.id).update(
                        likes_count=F("likes_count") + 1
                    )

                    is_liked = True

            logger.info(
                f"{TAG} | post={post.id} | actor_type={'user' if actor.is_user else 'org'} | actor_id={actor.user.id if actor.is_user else actor.organization.id} | liked={is_liked}"
            )

            return response_data(
                success=True,
                message="Success",
                data={
                    "post_id": str(post.id),
                    "is_liked": is_liked
                }
            )

        except Exception as e:
            logger.error(f"{TAG} | Error | {str(e)}")

            return response_data(
                success=False,
                message="Something went wrong",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                error=str(e)
            )
        


class ListPostLikesAPIView(BaseAPIView):

    def get(self, request):
        TAG = "ListPostLikesAPIView"

        try:
            post_id = request.query_params.get("post_id")
            search = request.query_params.get("search", "").strip()

            limit = int(request.query_params.get("limit", 20))
            offset = int(request.query_params.get("offset", 0))

            limit = min(limit, 50)
            offset = max(offset, 0)

            # -------------------------
            #  VALIDATION
            # -------------------------
            if not post_id:
                return response_data(False, "post_id is required", status_code=400)

            # -------------------------
            #  BASE QUERY
            # -------------------------
            queryset = Like.objects.filter(post_id=post_id)

            # -------------------------
            # SEARCH
            # -------------------------
            if search:
                queryset = queryset.filter(
                    Q(user__username__icontains=search) |
                    Q(user__profile__name__icontains=search) |
                    Q(organization__name__icontains=search)
                )

            total_count = queryset.count()

            # -------------------------
            # OPTIMIZATION
            # -------------------------
            queryset = queryset.select_related(
                "user__profile",
                "organization"
            ).order_by("-created_at")[offset: offset + limit]

            # -------------------------
            #  SERIALIZE
            # -------------------------
            serializer = LikeListSerializer(queryset, many=True)

            logger.info(f"{TAG} | post={post_id} | count={len(serializer.data)}")

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
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                error=str(e)
            )