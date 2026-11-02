import logging
from django.db import transaction
from django.db.models import Q, F
from rest_framework import status
from rest_framework.exceptions import ValidationError

from core.views.base_views import BaseAPIView
from apps.accounts.models import User
from apps.posts.models import Post, Like, Comment
from apps.sports.models import Sport
from utils.response import response_data
from apps.moderation.selectors.profile_visibility import hide_if_blocked_by_id
from apps.moderation.selectors.blocked_filters import exclude_blocked
from apps.connections.models import Follow
from core.constant import TYPE_USER
from apps.posts.serializers.posts_serializers import (
    PostListSerializer, POST_MENTIONS_PREFETCH,
)
from services.location.location_service import LocationService
from apps.posts.selectors.post_visibility_selectors import profile_visibility_filter
from apps.posts.services.post_service import (
    PostCreateError,
    PostService,
    notify_new_mentions,
)
from apps.posts.services.post_content_service import sync_post_content
from apps.media.models import PendingPublication
from apps.media.serializers.publication_serializers import (
    PendingPublicationSerializer,
)
from apps.media.services.publication_service import PublicationService
from apps.posts.services.saved_post_service import annotate_is_saved
from apps.organization.services.user_organization_services import UserOrganizationService


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
        ],
        "location": {
            "provider": "google",          # optional, defaults to google
            "external_id": "ChIJ...",      # Google place_id
            "name": "Kannur Stadium",
            "type": "place",
            "city": "Kannur",
            "state": "Kerala",
            "country": "India",
            "country_code": "IN",
            "latitude": 11.868,
            "longitude": 75.355
        }
    }

    A media item may instead be {"media_job_id": "<uuid>", "order": n} — an
    original the media worker converted (apps/media). Every job ready → the
    post is created now: 200 {"post_id"}, exactly as for browser-made files.
    Any job still converting → 202 {"pending_publication": {...}}: the body
    has passed every check, and the server publishes the post itself when the
    jobs finish. Validation lives in PostService; this view only routes.
    '''
    def post(self, request):
        TAG = "CreatePostAPIView"

        try:
            actor = request.actor
            user = request.user
            data = request.data

            try:
                cleaned = PostService.validate_create_payload(actor, user, data)

                # Media still converting on the media worker: hold the post
                # and let the server publish it when the jobs finish
                # (PublicationService.try_publish). Nothing is created now.
                if cleaned["pending_jobs"]:
                    publication = PublicationService.create(
                        actor=actor,
                        user=user,
                        target=PendingPublication.Target.POST,
                        payload=cleaned["payload"],
                        jobs=cleaned["jobs"],
                    )
                    logger.info(
                        f"{TAG} | Post held for processing | "
                        f"publication_id={publication.id}"
                    )
                    return response_data(
                        success=True,
                        message="Your post is processing",
                        status_code=status.HTTP_202_ACCEPTED,
                        data={
                            "pending_publication":
                                PendingPublicationSerializer(publication).data
                        },
                    )

                post = PostService.create_post(actor, user, cleaned)

            except PostCreateError as e:
                return response_data(
                    False,
                    message=e.message,
                    status_code=e.status_code,
                    error=e.error,
                )

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



class UpdatePostAPIView(BaseAPIView):
    '''
    Edit a post's text fields only — media is never touched here.

    PATCH /posts/update
    {
        "post_id": "uuid",
        "content": "updated text",
        "visibility": "public" | "followers",
        "sport_id": "uuid" | null,          # null clears the sport
        "location": { ...place... } | null   # omit = unchanged, null = clear
    }
    '''
    def patch(self, request):
        TAG = "UpdatePostAPIView"

        try:
            actor = request.actor
            data = request.data
            post_id = data.get("post_id")

            if not post_id:
                return response_data(False, message="post_id is required", status_code=400)

            # -------------------------
            # OWNERSHIP (active actor must be the author)
            # -------------------------
            if actor.is_user:
                post = Post.objects.filter(
                    id=post_id, author_user=actor.user, is_deleted=False
                ).first()
            elif actor.is_org:
                post = Post.objects.filter(
                    id=post_id, author_org=actor.organization, is_deleted=False
                ).first()
            else:
                return response_data(False, message="Invalid actor", status_code=400)

            if not post:
                return response_data(False, message="Post not found", status_code=404)

            with transaction.atomic():
                # -------------------------
                # CONTENT
                # -------------------------
                content_changed = "content" in data
                if content_changed:
                    content = (data.get("content") or "").strip()
                    if not content and not post.media.exists():
                        return response_data(False, message="Post cannot be empty", status_code=400)
                    post.content = content

                # -------------------------
                # VISIBILITY
                # -------------------------
                if "visibility" in data:
                    visibility = data.get("visibility")
                    if visibility not in Post.Visibility.values:
                        return response_data(False, message="Invalid visibility", status_code=400)
                    post.visibility = visibility

                # -------------------------
                # SPORT  (null / "" clears)
                # -------------------------
                if "sport_id" in data:
                    sport_id = data.get("sport_id")
                    if sport_id:
                        sport = Sport.objects.filter(id=sport_id).only("id").first()
                        if not sport:
                            return response_data(False, message="Invalid sport_id", status_code=400)
                        post.sport = sport
                    else:
                        post.sport = None

                # -------------------------
                # LOCATION  (absent = unchanged, null = clear, dict = set)
                # -------------------------
                if "location" in data:
                    location_data = data.get("location")

                    if location_data:
                        if not isinstance(location_data, dict):
                            return response_data(False, message="Invalid location format", status_code=400)
                        has_coords = (
                            location_data.get("latitude") is not None
                            and location_data.get("longitude") is not None
                        )
                        if not has_coords and not location_data.get("external_id"):
                            return response_data(False, message="Location must include latitude and longitude", status_code=400)

                        try:
                            location = LocationService.get_or_create_location(location_data)
                        except ValueError as ve:
                            return response_data(False, error=str(ve), status_code=400)

                        denorm = LocationService.build_denormalized(location)
                        post.location = denorm.get("location")
                        post.location_name = denorm.get("location_name", "")
                        post.city = denorm.get("city", "")
                        post.country_code = denorm.get("country_code", "")
                        post.latitude = denorm.get("latitude")
                        post.longitude = denorm.get("longitude")
                    else:
                        post.location = None
                        post.location_name = ""
                        post.city = ""
                        post.country_code = ""
                        post.latitude = None
                        post.longitude = None

                post.save()

                # Only the body carries hashtags and mentions, so an edit that
                # changes just the sport/visibility/location leaves the rows
                # alone. Only NEWLY added mentions are notified — re-saving the
                # same text tells nobody twice.
                if content_changed:
                    added_mentions = sync_post_content(post)
                    notify_new_mentions(post, actor, added_mentions)

            # -------------------------
            # SERIALIZE (with this actor's reaction)
            # -------------------------
            post = (
                annotate_is_saved(Post.objects.all(), actor)
                .select_related("author_user__profile", "author_org", "sport")
                .prefetch_related("media", POST_MENTIONS_PREFETCH)
                .get(id=post.id)
            )

            user_reactions = {}
            if actor.is_user:
                reaction = Like.objects.filter(user=actor.user, post_id=post.id).values("post_id", "type").first()
            else:
                reaction = Like.objects.filter(organization=actor.organization, post_id=post.id).values("post_id", "type").first()
            if reaction:
                user_reactions[reaction["post_id"]] = reaction["type"]

            serializer = PostListSerializer(post, context={"user_reactions": user_reactions})

            logger.info(f"{TAG} | Post updated | post_id={post.id}")

            return response_data(
                success=True,
                message="Post updated successfully",
                data=serializer.data,
            )

        except Exception as e:
            logger.error(f"{TAG} | Error | {str(e)}")
            return response_data(
                success=False,
                message="Something went wrong",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                error=str(e),
            )




class ListPostsAPIView(BaseAPIView):

    def get(self, request):
        TAG = "ListPostsAPIView"

        try:
            actor = request.actor

            post_id = request.query_params.get("post_id")
            username = request.query_params.get("username")
            sport_id = request.query_params.get("sport_id")

            limit = min(int(request.query_params.get("limit", 10)), 50)
            offset = max(int(request.query_params.get("offset", 0)), 0)

            # -------------------------
            # VALIDATION
            # -------------------------
            if not username and not post_id:
                return response_data(False, "username or post_id is required", status_code=400)

            # -------------------------
            # BASE QUERY
            # -------------------------
            queryset = Post.objects.filter(is_deleted=False)

            if post_id:
                queryset = queryset.filter(id=post_id)

            profile = None

            # -------------------------
            # PROFILE RESOLUTION
            # -------------------------
            if username:
                try:
                    profile = UserOrganizationService.get_user_or_org_by_username(username)
                except ValueError as e:
                    return response_data(False, str(e), status_code=404)

                # §1.5 — the owner blocked this viewer: the profile does
                # not exist for them, and neither does its post list. Same 404
                # the unknown-handle branch above returns.
                if hide_if_blocked_by_id(actor, profile["id"], profile["type"]):
                    return response_data(
                        False, "Profile not found", status_code=404
                    )

                if profile["type"] == TYPE_USER:
                    queryset = queryset.filter(author_user_id=profile["id"])
                else:
                    queryset = queryset.filter(author_org_id=profile["id"])

            if sport_id:
                queryset = queryset.filter(sport_id=sport_id)

            # -------------------------
            # VISIBILITY
            # -------------------------
            # The rule now lives in posts.selectors.post_visibility_selectors so
            # the public (logged-out) profile list runs the identical filter
            # instead of a second copy. Behaviour here is unchanged.
            queryset = queryset.filter(
                profile_visibility_filter(actor, profile)
            )

            # THE OTHER DIRECTION — this viewer blocked the owner, so they get
            # the shell with an empty grid rather than a 404 (they know why).
            queryset = exclude_blocked(queryset, actor)

            total_count = queryset.count()

            # -------------------------
            # OPTIMIZATION
            # -------------------------
            queryset = annotate_is_saved(queryset, actor).select_related(
                "author_user__profile",
                "author_org",
                "sport"
            ).prefetch_related("media", POST_MENTIONS_PREFETCH)

            queryset = queryset.order_by("-created_at")[offset: offset + limit]

            # -------------------------
            # USER REACTIONS
            # -------------------------
            post_ids = list(queryset.values_list("id", flat=True))
            user_reactions = {}

            if actor.is_user:
                reactions = Like.objects.filter(
                    user=actor.user,
                    post_id__in=post_ids
                ).values("post_id", "type")

            else:
                reactions = Like.objects.filter(
                    organization=actor.organization,
                    post_id__in=post_ids
                ).values("post_id", "type")

            user_reactions = {
                r["post_id"]: r["type"]
                for r in reactions
            }

            # -------------------------
            # SERIALIZE
            # -------------------------
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




class DeletePost(BaseAPIView):

    def delete(self, request):
        try:
            actor = request.actor
            post_id = request.query_params.get('post_id')

            if not post_id:
                return response_data(
                    success=False,
                    error="post id is required"
                )
            is_deleted, _ = PostService.delete_post(
                post_id=post_id,
                actor=actor
            )

            print(is_deleted, '----')

            return response_data(
                success=True if is_deleted else False,
                message="post deleted successfully" if is_deleted else "post not deleted",
                status_code=200 if is_deleted else 400
            )

        except Exception as e:
            return response_data(
                success=False,
                error=f"failed to delete due to server error str(e)"
            )