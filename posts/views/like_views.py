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

from django.db import transaction

class ToggleLikeAPIView(BaseAPIView):

    def post(self, request):
        TAG = "ToggleLikeAPIView"

        try:
            actor = request.actor
            post_id = request.data.get("post_id")
            liked_type = request.data.get("type", Like.Type.LIKE)

            # VALIDATION
            if not post_id:
                return response_data(False, "post_id is required", status_code=400)

            if liked_type not in Like.Type.values:
                return response_data(False, "Invalid like type", status_code=400)

            # TRANSACTION START
            with transaction.atomic():

                # LOCK inside transaction
                post = Post.objects.select_for_update().filter(
                    id=post_id,
                    is_deleted=False
                ).first()

                if not post:
                    return response_data(False, "Post not found", status_code=404)

                # BUILD FILTER
                like_filter = {"post": post}

                if actor.is_user:
                    like_filter["user"] = actor.user
                else:
                    like_filter["organization"] = actor.organization

                existing_like = Like.objects.filter(**like_filter).first()

                breakdown = post.likes_breakdown or {}

                # CASE 1: REMOVE LIKE
                if existing_like and existing_like.type == liked_type:
                    old_type = existing_like.type

                    existing_like.delete()

                    post.likes_count = max(0, post.likes_count - 1)
                    breakdown[old_type] = max(0, breakdown.get(old_type, 1) - 1)

                    is_liked = False
                    current_type = None

                # CASE 2: CHANGE TYPE
                elif existing_like:
                    old_type = existing_like.type

                    existing_like.type = liked_type
                    existing_like.save(update_fields=["type"])

                    breakdown[old_type] = max(0, breakdown.get(old_type, 1) - 1)
                    breakdown[liked_type] = breakdown.get(liked_type, 0) + 1

                    is_liked = True
                    current_type = liked_type

                # CASE 3: NEW LIKE
                else:
                    Like.objects.create(**like_filter, type=liked_type)

                    post.likes_count += 1
                    breakdown[liked_type] = breakdown.get(liked_type, 0) + 1

                    is_liked = True
                    current_type = liked_type

                # SAVE POST
                post.likes_breakdown = breakdown
                post.save(update_fields=["likes_count", "likes_breakdown"])

            # RESPONSE
            logger.info(
                f"{TAG} | post={post.id} | actor_type={'user' if actor.is_user else 'org'} | liked={is_liked} | type={current_type}"
            )

            return response_data(
                success=True,
                message="Success",
                data={
                    "post_id": str(post.id),
                    "is_liked": is_liked,
                    "type": current_type,
                    "likes_count": post.likes_count,
                    "likes_breakdown": post.likes_breakdown
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