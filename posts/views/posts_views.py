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
from posts.serializers.posts_serializers import PostListSerializer

logger = logging.getLogger(__name__)


class CreatePostAPIView(BaseAPIView):
    '''
    {
        "content": "🔥 Football trial highlights! Looking for strikers ⚽",
        "post_type": "normal",
        "visibility": "public",
        "sport_id": "uuid",

        "media": [
            {
            "file_url": "https://goatza/post1.jpg",
            "media_type": "image",
            "order": 0
            },
            {
            "file_url": "https:/goatza/video1.mp4",
            "media_type": "video",
            "thumbnail_url": "https://goatza/video1-thumb.jpg",
            "duration": 32,
            "order": 1
            }
        ]
        }
    '''
    def post(self, request):
        TAG = "CreatePostAPIView"

        try:
            actor = request.actor
            data = request.data

            content = (data.get("content") or "").strip()
            post_type = data.get("post_type", Post.PostType.NORMAL)
            visibility = data.get("visibility", Post.Visibility.PUBLIC)
            sport_id = data.get("sport_id")
            media_list = data.get("media", [])

            # -------------------------
            # VALIDATIONS
            # -------------------------

            # At least content or media required
            if not content and not media_list:
                return response_data(
                    success=False,
                    message="Post cannot be empty",
                    status_code=400
                )

            # Validate post_type
            if post_type not in Post.PostType.values:
                return response_data(
                    success=False,
                    message="Invalid post_type",
                    status_code=400
                )

            # Validate visibility
            if visibility not in Post.Visibility.values:
                return response_data(
                    success=False,
                    message="Invalid visibility",
                    status_code=400
                )

            # Validate sport
            sport = None
            if sport_id:
                sport = Sport.objects.filter(id=sport_id).only("id").first()
                if not sport:
                    return response_data(
                        success=False,
                        message="Invalid sport_id",
                        status_code=400
                    )

            # Validate media list
            if not isinstance(media_list, list):
                return response_data(
                    success=False,
                    message="media must be a list",
                    status_code=400
                )

            if len(media_list) > 10:
                return response_data(
                    success=False,
                    message="Maximum 10 media allowed",
                    status_code=400
                )

            valid_media_types = {PostMedia.MediaType.IMAGE, PostMedia.MediaType.VIDEO}

            for idx, media in enumerate(media_list):
                if not isinstance(media, dict):
                    return response_data(False, f"Invalid media at index {idx}", status_code=400)

                file_url = media.get("file_url")
                media_type = media.get("media_type")

                if not file_url:
                    return response_data(False, f"file_url required at index {idx}", status_code=400)

                if media_type not in valid_media_types:
                    return response_data(False, f"Invalid media_type at index {idx}", status_code=400)

                # Video-specific validation
                if media_type == PostMedia.MediaType.VIDEO:
                    duration = media.get("duration")
                    if duration is None:
                        return response_data(False, f"duration required for video at index {idx}", status_code=400)

            # -------------------------
            # CREATE POST
            # -------------------------

            with transaction.atomic():

                post = Post.objects.create(
                    author_user=actor.user if actor.is_user else None,
                    author_org=actor.organization if actor.is_org else None,
                    content=content,
                    post_type=post_type,
                    visibility=visibility,
                    sport=sport
                )

                # Media bulk create
                media_objs = []
                for index, media in enumerate(media_list):
                    media_objs.append(
                        PostMedia(
                            post=post,
                            file_url=media.get("file_url"),
                            media_type=media.get("media_type"),
                            thumbnail_url=media.get("thumbnail_url", ""),
                            duration=media.get("duration"),
                            order=media.get("order", index)
                        )
                    )

                if media_objs:
                    PostMedia.objects.bulk_create(media_objs)

            logger.info(f"{TAG} | Post created | post_id={post.id} | actor={request.user.id}")

            return response_data(
                success=True,
                message="Post created successfully",
                data={"post_id": str(post.id)}
            )

        except ValidationError as e:
            logger.warning(f"{TAG} | Validation error | {str(e)}")
            return response_data(
                success=False,
                message="Validation error",
                status_code=400,
                error=str(e)
            )

        except Exception as e:
            logger.error(f"{TAG} | Error | {str(e)}")

            return response_data(
                success=False,
                message="Something went wrong",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                error=str(e)
            )
        





class ListPostsAPIView(BaseAPIView):

    def get(self, request):
        TAG = "ListPostsAPIView"

        try:
            actor = request.actor

            # -------------------------
            # 🔹 Query params
            # -------------------------
            user_id = request.query_params.get("user_id")
            org_id = request.query_params.get("org_id")
            sport_id = request.query_params.get("sport_id")

            limit = int(request.query_params.get("limit", 20))
            offset = int(request.query_params.get("offset", 0))

            limit = min(limit, 50)
            offset = max(offset, 0)

            # -------------------------
            # VALIDATION
            # -------------------------
            if not user_id and not org_id:
                return response_data(False, "user_id or org_id is required", status_code=400)

            if user_id and org_id:
                return response_data(False, "Provide either user_id or org_id", status_code=400)

            # -------------------------
            # BASE QUERY
            # -------------------------
            queryset = Post.objects.filter(is_deleted=False)

            if user_id:
                queryset = queryset.filter(author_user_id=user_id)

            if org_id:
                queryset = queryset.filter(author_org_id=org_id)

            if sport_id:
                queryset = queryset.filter(sport_id=sport_id)

            # -------------------------
            # VISIBILITY
            # -------------------------
            is_following_user = False
            is_following_org = False

            if user_id:
                if actor.is_user:
                    is_following_user = Follow.objects.filter(
                        follower_user=actor.user,
                        following_user_id=user_id
                    ).exists()
                else:
                    is_following_user = Follow.objects.filter(
                        follower_org=actor.organization,
                        following_user_id=user_id
                    ).exists()

            if org_id:
                if actor.is_user:
                    is_following_org = Follow.objects.filter(
                        follower_user=actor.user,
                        following_org_id=org_id
                    ).exists()
                else:
                    is_following_org = Follow.objects.filter(
                        follower_org=actor.organization,
                        following_org_id=org_id
                    ).exists()

            visibility_filter = Q(visibility=Post.Visibility.PUBLIC)

            if user_id and is_following_user:
                visibility_filter |= Q(
                    visibility=Post.Visibility.FOLLOWERS,
                    author_user_id=user_id
                )

            if org_id and is_following_org:
                visibility_filter |= Q(
                    visibility=Post.Visibility.FOLLOWERS,
                    author_org_id=org_id
                )

            # own posts always visible
            if actor.is_user:
                visibility_filter |= Q(author_user=actor.user)
            else:
                visibility_filter |= Q(author_org=actor.organization)

            queryset = queryset.filter(visibility_filter)

            total_count = queryset.count()

            # -------------------------
            # OPTIMIZATION
            # -------------------------
            queryset = queryset.select_related(
                "author_user__profile",
                "author_org",
                "sport"
            ).prefetch_related("media")

            queryset = queryset.order_by("-created_at")[offset: offset + limit]

            # -------------------------
            # LIKED POSTS
            # -------------------------
            post_ids = [post.id for post in queryset]

            if actor.is_user:
                liked_post_ids = set(
                    Like.objects.filter(
                        user=actor.user,
                        post_id__in=post_ids
                    ).values_list("post_id", flat=True)
                )
            else:
                liked_post_ids = set()  # org like disabled (recommended)

            # -------------------------
            # SERIALIZE
            # -------------------------
            serializer = PostListSerializer(
                queryset,
                many=True,
                context={"liked_post_ids": liked_post_ids}
            )

            logger.info(f"{TAG} | Success | count={len(serializer.data)}")

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
        
