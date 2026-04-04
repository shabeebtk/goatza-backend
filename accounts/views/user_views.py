import logging
from rest_framework.views import APIView
from rest_framework import serializers
from django.db import transaction
from accounts.models import (
    User, UserProfile
)
from rest_framework.permissions import IsAuthenticated
from accounts.serializers.user_serializers import UserSerializer, UserFullSerializer, UpdateUserMediaSerializer
from utils.response import response_data
from utils.cache import cache_set, cache_get, cache_delete
from utils.cache_keys import CacheKeys
from connections.services.follow_services import FollowService
from services.storage.factory import get_storage_service
from services.storage.validators import validate_media, DEFAULT_IMAGE_EXTENSIONS
from accounts.serializers.user_update_serilizer import UpdateUserProfileSerializer
from utils.validations import validate_username_format

logger = logging.getLogger(__name__)


class GetUserDetails(APIView):
    permission_classes = [IsAuthenticated]

    LIST_TYPE_MINI = 'mini'
    LIST_TYPE_FULL = 'full'

    def get(self, request, username):
        list_type = request.query_params.get("list_type", self.LIST_TYPE_MINI)
        viewer = request.user

        try:
            if list_type == self.LIST_TYPE_FULL:
                user = (
                    User.objects
                    .select_related("profile")
                    .prefetch_related(
                        "sports__sport",
                        "positions__position",
                        "positions__sport"
                    )
                    .get(username=username)
                )

                serializer = UserFullSerializer(user)
            else:

                user = User.objects.select_related("profile").get(username=username)
                serializer = UserSerializer(user)

            user_data = serializer.data
            user_id = user.id

            relation = FollowService.get_relationship(viewer, user_id)
            user_data.update({"relationship" : relation})

            return response_data(success=True, data=user_data)
        except User.DoesNotExist as e:
            return response_data(
                False,
                "User not found",
                status_code=404
            )
        except Exception as e:
            return response_data(
                success=False,
                error=f"failed to get user : {str(e)}",
                status_code=500
            )



class GetUserDetailsByID(APIView):
    permission_classes = [IsAuthenticated]

    LIST_TYPE_MINI = 'mini'
    LIST_TYPE_FULL = 'full'
    
    def get(self, request):
        try:
            list_type = request.query_params.get("list_type")
            user_id = request.user.id

            # Force optimized query
            user = User.objects.select_related("profile").get(id=user_id)

            if list_type == self.LIST_TYPE_FULL:
                serializer = UserFullSerializer(user)
            else:
                serializer = UserSerializer(user)

            data = serializer.data
            return response_data(success=True, data=data)
        
        except Exception as e:
            return response_data(
                success=False,
                error=f"failed to get user : {str(e)}",
                status_code=500
            )
        


class CheckUsernameAvailabilityAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            username = request.query_params.get("username")

            if not username:
                return response_data(
                    False,
                    message="username is required",
                    status_code=400
                )

            username = str(username).strip().lower()

            try:
                username = validate_username_format(username)
            except ValueError as ve:
                return response_data(
                    False,
                    message=str(ve),
                    status_code=400
                )

            user = request.user

            # check
            exists = User.objects.filter(
                username__iexact=username
            ).exclude(id=user.id).exists()

            logger.debug(
                f"[USERNAME CHECK] user={user.id}, username={username}, exists={exists}"
            )

            return response_data(
                True,
                message="Username available" if not exists else "Username already taken",
                data={
                    "username": username,
                    "available": not exists
                }
            )

        except Exception as e:
            logger.exception("[USERNAME CHECK] error")

            return response_data(
                False,
                message="Failed to check username",
                error=str(e),
                status_code=500
            )


class UpdateUserMediaAPIView(APIView):
    '''
    upload
    {
        "profile_photo": "https://res.cloudinary.com/.../profile.jpg",
        "profile_photo_public_id": "users/123/profile",

        "cover_photo": "https://res.cloudinary.com/.../cover.jpg",
        "cover_photo_public_id": "users/123/cover"
    }

    delete 
    {
        "is_delete_profile": true,
        "is_delete_cover": true
    }
    '''
    permission_classes = [IsAuthenticated]

    def post(self, request):
        try:
            serializer = UpdateUserMediaSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)

            try:
                profile = request.user.profile
            except UserProfile.DoesNotExist:
                return response_data(False, error="Profile not found", status_code=404)

            storage = get_storage_service()
            data = serializer.validated_data

            update_fields = []

            #  DELETE PROFILE PHOTO
            if data.get("is_delete_profile"):
                if profile.profile_photo_public_id:
                    storage.delete_file(profile.profile_photo_public_id)

                profile.profile_photo = ""
                profile.profile_photo_public_id = ""
                update_fields += ["profile_photo", "profile_photo_public_id"]

            # DELETE COVER PHOTO
            if data.get("is_delete_cover"):
                if profile.cover_photo_public_id:
                    storage.delete_file(profile.cover_photo_public_id)

                profile.cover_photo = ""
                profile.cover_photo_public_id = ""
                update_fields += ["cover_photo", "cover_photo_public_id"]

            # UPDATE PROFILE PHOTO
            if "profile_photo" in data:
                validate_media(
                    request.user,
                    data["profile_photo"],
                    data["profile_photo_public_id"],
                    allowed_extensions=DEFAULT_IMAGE_EXTENSIONS
                )
                profile.profile_photo = data["profile_photo"]
                profile.profile_photo_public_id = data["profile_photo_public_id"]

                update_fields += ["profile_photo", "profile_photo_public_id"]

            # UPDATE COVER PHOTO
            if "cover_photo" in data:
                validate_media(
                    request.user,
                    data["cover_photo"],
                    data["cover_photo_public_id"],
                    allowed_extensions=DEFAULT_IMAGE_EXTENSIONS
                )

                profile.cover_photo = data["cover_photo"]
                profile.cover_photo_public_id = data["cover_photo_public_id"]

                update_fields += ["cover_photo", "cover_photo_public_id"]

            if update_fields:
                update_fields.append("updated_at")
                profile.save(update_fields=update_fields)

            return response_data(success=True, message="Media updated successfully")

        except ValueError as ve:
            return response_data(success=False, error=str(ve), status_code=400)

        except serializers.ValidationError as se:
            return response_data(success=False, error=str(se), status_code=400)

        except Exception as e:
            return response_data(
                False,
                error=f"Failed to update media: {str(e)}",
                status_code=500
            )
        


class UpdateUserProfileAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def patch(self, request):
        TAG = "[PROFILE UPDATE]"
        user = request.user

        logger.info(f"{TAG} User={user.id} request started")

        try:
            serializer = UpdateUserProfileSerializer(
                data=request.data,
                context={"request": request}
            )
            serializer.is_valid(raise_exception=True)

            data = serializer.validated_data
            profile = user.profile

            user_fields = []
            profile_fields = []

            logger.debug(f"{TAG} Payload={data}")

            # USER FIELDS
            if "username" in data:
                old_username = user.username
                user.username = data["username"]
                user_fields.append("username")

                logger.info(
                    f"{TAG} Username changed: {old_username} → {data['username']}"
                )

            # PROFILE FIELDS
            profile_mapping = [
                "name",
                "headline",
                "about",
                "gender",
                "birthdate",
                "height_cm",
                "weight_kg",
            ]

            for field in profile_mapping:
                if field in data:
                    old_value = getattr(profile, field)
                    new_value = data[field]

                    setattr(profile, field, new_value)
                    profile_fields.append(field)

                    logger.debug(
                        f"{TAG} {field}: {old_value} → {new_value}"
                    )

            # ATOMIC SAVE
            with transaction.atomic():
                if user_fields:
                    user.save(update_fields=user_fields + ["updated_at"])

                if profile_fields:
                    profile.save(update_fields=profile_fields + ["updated_at"])

            updated_fields = user_fields + profile_fields

            logger.info(
                f"{TAG} Success user={user.id}, fields={updated_fields}"
            )

            # Return the full updated profile so the frontend can seed its cache correctly
            user_fresh = (
                User.objects
                .select_related("profile")
                .prefetch_related(
                    "sports__sport",
                    "positions__position",
                    "positions__sport"
                )
                .get(id=user.id)
            )
            response_serializer = UserFullSerializer(user_fresh)

            return response_data(
                success=True,
                message="Profile updated successfully",
                data=response_serializer.data
            )

        except serializers.ValidationError as e:
            logger.warning(
                f"{TAG} Validation failed user={user.id}, error={e.detail}"
            )

            return response_data(
                success=False,
                message="Validation failed",
                data=e.detail,
                status_code=400
            )

        except Exception as e:
            logger.exception(
                f"{TAG} Unexpected error user={user.id}"
            )
            return response_data(
                success=False,
                message="Failed to update profile",
                error=str(e),
                status_code=500
            )