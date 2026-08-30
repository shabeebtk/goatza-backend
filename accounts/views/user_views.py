import logging
from rest_framework.views import APIView
from core.views.base_views import BaseAPIView
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
from moderation.selectors.profile_visibility import (
    hide_if_blocked,
    profile_block_state,
)
from services.storage.factory import get_storage_service
from services.storage.validators import (
    allowed_image_extensions,
    validate_media,
    with_cache_buster,
)
from accounts.serializers.user_update_serilizer import UpdateUserProfileSerializer
from services.location.location_service import LocationService
from usernames.exceptions import UsernameTaken
from usernames.services.username_service import UsernameService
from utils.validations import validate_username_format
from core.constant import TYPE_USER
from core.actor import Actor

logger = logging.getLogger(__name__)


class GetUserDetails(BaseAPIView):
    LIST_TYPE_MINI = 'mini'
    LIST_TYPE_FULL = 'full'

    def get(self, request, username):
        list_type = request.query_params.get("list_type", self.LIST_TYPE_MINI)
        actor = request.actor

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

                # actor in context → highlights_count for the chip (one COUNT)
                serializer = UserFullSerializer(user, context={"actor": actor})
            else:

                user = User.objects.select_related("profile").get(username=username)
                serializer = UserSerializer(user)

            # §1.5 — a profile that blocked THIS viewer does not exist for
            # them. Raises User.DoesNotExist, so the handler below renders the
            # exact same 404 an unknown username does.
            hide_if_blocked(user, actor)

            user_data = serializer.data
            user_id = user.id

            relation = FollowService.get_relationship(
                actor=actor,
                target_id=user.id,
                target_type=TYPE_USER
            )
            user_data.update({"relationship" : relation})

            # The OTHER direction: the blocker sees a shell plus the flag that
            # drives "You blocked this account" + Unblock.
            user_data.update(profile_block_state(actor, user))

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
                # own profile — the owner sees every one of their highlights
                serializer = UserFullSerializer(
                    user,
                    context={"actor": Actor(actor_type=TYPE_USER, user=user)}
                )
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

            user = request.user

            # INVALID and TAKEN are different answers and the UI says different
            # things about them, so they get different shapes: a 400 with the
            # reason for a handle that could never be allowed, a 200 with
            # available=false for one that is merely spoken for.
            try:
                available = UsernameService.is_available(
                    username, exclude_user=user
                )
            except ValueError as ve:
                return response_data(
                    False,
                    message=str(ve),
                    data={"username": str(username).strip().lower(), "valid": False},
                    status_code=400
                )

            username = validate_username_format(username)

            logger.debug(
                f"[USERNAME CHECK] user={user.id}, username={username}, "
                f"available={available}"
            )

            return response_data(
                True,
                message="Username available" if available else "Username already taken",
                data={
                    "username": username,
                    "valid": True,
                    "available": available
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
        "profile_photo": "https://media.goatza.com/users/<id>/profile.webp",
        "profile_photo_public_id": "users/123/profile",

        "cover_photo": "https://media.goatza.com/users/<id>/cover.webp",
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
            #
            # profile/cover each own ONE key per user and are overwritten in
            # place, so the URL never changes and the CDN keeps serving the old
            # image. with_cache_buster stamps ?v=<ts> on the stored URL; the
            # public_id column keeps the bare key, which is what a later
            # delete_file needs.
            if "profile_photo" in data:
                validate_media(
                    request.user,
                    data["profile_photo"],
                    data["profile_photo_public_id"],
                    allowed_extensions=allowed_image_extensions()
                )
                profile.profile_photo = with_cache_buster(data["profile_photo"])
                profile.profile_photo_public_id = data["profile_photo_public_id"]

                update_fields += ["profile_photo", "profile_photo_public_id"]

            # UPDATE COVER PHOTO
            if "cover_photo" in data:
                validate_media(
                    request.user,
                    data["cover_photo"],
                    data["cover_photo_public_id"],
                    allowed_extensions=allowed_image_extensions()
                )

                profile.cover_photo = with_cache_buster(data["cover_photo"])
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

            # Handles are NOT a plain field write — they live in the shared
            # UsernameRegistry, so the claim happens inside the atomic save
            # below (it writes the display column itself and busts the old AND
            # new lookup caches). Tracked separately from user_fields for the
            # same reason: nothing else should re-save the column.
            claims_username = "username" in data

            # LOCATION UPDATE
            if "location" in data:
                location_data = data["location"]

                if location_data is None:
                    profile.location = None
                    profile.location_name = ""
                    profile.city = ""
                    profile.country_code = ""
                    profile.latitude = None
                    profile.longitude = None

                    profile_fields.extend([
                        "location",
                        "location_name",
                        "city",
                        "country_code",
                        "latitude",
                        "longitude"
                    ])
                    logger.info(f"{TAG} Location cleared")
                else:
                    try:
                        location = LocationService.get_or_create_location(location_data)
                    except ValueError as e:
                        # Out-of-range or missing coordinates on a place with no
                        # row on file. A 400 with the field name, not the 500
                        # the generic handler below would turn it into.
                        return response_data(
                            success=False,
                            message="Validation failed",
                            data={"location": [str(e)]},
                            status_code=400
                        )

                    denorm = LocationService.build_denormalized(location)
                    profile.location = location
                    profile.location_name = denorm["location_name"]
                    profile.city = denorm["city"]
                    profile.country_code = denorm["country_code"]
                    profile.latitude = denorm["latitude"]
                    profile.longitude = denorm["longitude"]

                    profile_fields.extend([
                        "location",
                        "location_name",
                        "city",
                        "country_code",
                        "latitude",
                        "longitude"
                    ])
                    logger.info(f"{TAG} Location updated → {denorm['location_name']}")
                        

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
                if claims_username:
                    # Raises UsernameTaken (caught below) — deliberately NOT
                    # returned from in here, or the rollback would never run
                    # and a half-applied profile edit would commit.
                    UsernameService.claim(data["username"], user=user)

                if user_fields:
                    user.save(update_fields=user_fields + ["updated_at"])

                if profile_fields:
                    profile.save(update_fields=profile_fields + ["updated_at"])

            updated_fields = (
                (["username"] if claims_username else []) + user_fields + profile_fields
            )

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
            response_serializer = UserFullSerializer(
                user_fresh,
                context={"actor": Actor(actor_type=TYPE_USER, user=user_fresh)}
            )

            return response_data(
                success=True,
                message="Profile updated successfully",
                data=response_serializer.data
            )

        except UsernameTaken:
            # Lost the race between the serializer's pre-check and the insert.
            # The unique constraint is the arbiter, and this is what it said.
            logger.info(f"{TAG} Username taken user={user.id}")

            return response_data(
                success=False,
                message="Validation failed",
                data={"username": ["Username already taken"]},
                status_code=400
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


class SetUserRoleAPIView(APIView):
    """
    One-time onboarding action that lets a user set their role.

    Used by OAuth users who signed up without choosing a role (they land here with
    is_role_confirmed=False). This is NOT a general role editor — once the role is
    confirmed the endpoint rejects further changes, and role is deliberately absent
    from UpdateUserProfileSerializer so it can't be edited elsewhere.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        TAG = "[SET ROLE]"
        user = request.user
        role = request.data.get("role")

        if role not in User.Role.values:
            logger.warning(f"{TAG} Invalid role user={user.id}, role={role}")
            return response_data(False, "Invalid role", status_code=400)

        # Role stays editable throughout the onboarding window and is locked only
        # once onboarding is finished. A user still onboarding (is_onboarding_completed
        # False) may freely change their role; after that it's permanent.
        if user.is_role_confirmed and user.is_onboarding_completed:
            logger.warning(f"{TAG} Role locked (onboarding complete) user={user.id}")
            return response_data(False, "Role already set", status_code=400)

        user.role = role
        user.is_role_confirmed = True
        user.save(update_fields=["role", "is_role_confirmed", "updated_at"])

        logger.info(f"{TAG} Role set user={user.id}, role={role}")

        return response_data(
            success=True,
            message="Role updated successfully",
            data=UserSerializer(user).data
        )


class CompleteOnboardingAPIView(APIView):
    """
    Marks the post-signup onboarding flow as finished for the current user.

    Idempotent: calling it again once onboarding is already complete still returns
    success. After this succeeds the user's role becomes permanently locked (see
    SetUserRoleAPIView).
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        TAG = "[COMPLETE ONBOARDING]"
        user = request.user

        if user.is_onboarding_completed:
            logger.info(f"{TAG} Already complete user={user.id}")
            return response_data(
                success=True,
                message="Onboarding already completed",
                data=UserSerializer(user).data
            )

        user.is_onboarding_completed = True
        user.save(update_fields=["is_onboarding_completed", "updated_at"])

        logger.info(f"{TAG} Completed user={user.id}")

        return response_data(
            success=True,
            message="Onboarding completed",
            data=UserSerializer(user).data
        )