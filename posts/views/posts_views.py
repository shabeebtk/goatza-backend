import logging
from django.db import transaction
from django.db.models import Q, F
from rest_framework import status
from rest_framework.exceptions import ValidationError

from core.views.base_views import BaseAPIView
from accounts.models import User
from posts.models import Post, PostMedia, Like, Comment
from sports.models import Sport
from utils.response import response_data
from connections.models import Follow
from posts.serializers.posts_serializers import PostListSerializer
from services.storage.validators import validate_media, DEFAULT_IMAGE_EXTENSIONS, DEFAULT_VIDEO_EXTENSIONS


logger = logging.getLogger(__name__)


class CreatePostAPIView(BaseAPIView):
    '''
    {
        "content": "🔥 Football trial highlights! My performance ⚽",
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
            user = request.user
            data = request.data

            content = (data.get("content") or "").strip()
            post_type = data.get("post_type", Post.PostType.NORMAL)
            visibility = data.get("visibility", Post.Visibility.PUBLIC)
            sport_id = data.get("sport_id")
            media_list = data.get("media", [])

            # -------------------------
            # BASIC VALIDATIONS
            # -------------------------

            if not content and not media_list:
                return response_data(False, message="Post cannot be empty", status_code=400)

            if post_type not in Post.PostType.values:
                return response_data(False, message="Invalid post_type", status_code=400)

            if visibility not in Post.Visibility.values:
                return response_data(False, message="Invalid visibility", status_code=400)

            # Sport validation
            sport = None
            if sport_id:
                sport = Sport.objects.filter(id=sport_id).only("id").first()
                if not sport:
                    return response_data(False, message="Invalid sport_id", status_code=400)

            # -------------------------
            # MEDIA VALIDATION
            # -------------------------

            if not isinstance(media_list, list):
                return response_data(False, message="media must be a list", status_code=400)

            if len(media_list) > 10:
                return response_data(False, message="Maximum 10 media allowed", status_code=400)

            image_count = 0
            video_count = 0

            for idx, media in enumerate(media_list):

                if not isinstance(media, dict):
                    return response_data(False, f"Invalid media at index {idx}", status_code=400)

                file_url = media.get("file_url")
                media_type = media.get("media_type")
                public_id = media.get("public_id")

                if not file_url or not media_type or not public_id:
                    return response_data(False, f"Missing fields at index {idx}", status_code=400)

                # -------------------------
                # TYPE COUNT
                # -------------------------
                if media_type == PostMedia.MediaType.IMAGE:
                    image_count += 1
                elif media_type == PostMedia.MediaType.VIDEO:
                    video_count += 1
                else:
                    return response_data(False, f"Invalid media_type at index {idx}", status_code=400)

                # -------------------------
                # CLOUDINARY VALIDATION
                # -------------------------
                try:
                    if media_type == PostMedia.MediaType.IMAGE:
                        validate_media(
                            user,
                            file_url,
                            public_id,
                            allowed_extensions=DEFAULT_IMAGE_EXTENSIONS
                        )

                    elif media_type == PostMedia.MediaType.VIDEO:
                        validate_media(
                            user,
                            file_url,
                            public_id,
                            allowed_extensions=DEFAULT_VIDEO_EXTENSIONS
                        )

                        duration = media.get("duration")
                        if duration is None:
                            return response_data(False, f"duration required at index {idx}", status_code=400)

                        if duration > 300:
                            return response_data(False, "Video cannot exceed 5 minutes", status_code=400)

                except ValueError as ve:
                    return response_data(False, error=str(ve), status_code=400)

                # Order validation
                order = media.get("order", idx)
                if order < 0:
                    return response_data(False, "Invalid media order", status_code=400)

            # -------------------------
            # MEDIA RULES
            # -------------------------

            if image_count > 10:
                return response_data(False, "Max 10 images allowed", status_code=400)

            if video_count > 1:
                return response_data(False, "Only one video allowed", status_code=400)

            if video_count >= 1 and image_count >= 1:
                return response_data(False, "Cannot mix images and video", status_code=400)

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

                media_objs = []
                for idx, media in enumerate(media_list):
                    media_objs.append(
                        PostMedia(
                            post=post,
                            file_url=media.get("file_url"),
                            public_id=media.get("public_id"),
                            media_type=media.get("media_type"),
                            thumbnail_url=media.get("thumbnail_url", ""),
                            duration=media.get("duration"),
                            order=media.get("order", idx),
                        )
                    )

                if media_objs:
                    PostMedia.objects.bulk_create(media_objs)

            # Safe actor logging
            actor_id = actor.user.id if actor.is_user else actor.organization.id

            logger.info(f"{TAG} | Post created | post_id={post.id} | actor={actor_id}")

            return response_data(
                success=True,
                message="Post created successfully",
                data={"post_id": str(post.id)}
            )

        except ValidationError as e:
            logger.warning(f"{TAG} | Validation error | {str(e)}")
            return response_data(False, message="Validation error", status_code=400, error=str(e))

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

            username = request.query_params.get("username")
            user_id = request.query_params.get("user_id")
            org_id = request.query_params.get("org_id")
            sport_id = request.query_params.get("sport_id")

            limit = int(request.query_params.get("limit", 10))
            offset = int(request.query_params.get("offset", 0))

            limit = min(limit, 50)
            offset = max(offset, 0)

            # -------------------------
            # VALIDATION
            # -------------------------
            if not username and not user_id and not org_id:
                return response_data(False, "username or user_id or org_id is required", status_code=400)

            if (username and org_id) or (user_id and org_id):
                return response_data(False, "Provide either user or org", status_code=400)
            
            if username:
                user = User.objects.only("id").filter(username=username).first()
                if not user:
                    return response_data(False, "User not found", status_code=404)
                user_id = user.id

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
            # USER REACTIONS (FIXED)
            # -------------------------
            post_ids = list(queryset.values_list("id", flat=True))

            user_reactions = {}

            if actor.is_user:
                reactions = Like.objects.filter(
                    user=actor.user,
                    post_id__in=post_ids
                ).values("post_id", "type")

                user_reactions = {
                    r["post_id"]: r["type"]
                    for r in reactions
                }

            elif actor.is_org:
                reactions = Like.objects.filter(
                    organization=actor.organization,
                    post_id__in=post_ids
                ).values("post_id", "type")

                user_reactions = {
                    r["post_id"]: r["type"]
                    for r in reactions
                }

            # SERIALIZE
            serializer = PostListSerializer(
                queryset,
                many=True,
                context={"user_reactions": user_reactions}
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
        
