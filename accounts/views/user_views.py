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
from guardians.selectors.consent_selectors import guardian_status
from guardians.services.consent_service import ensure_pending_for_minor
from legal.selectors.acceptance_selectors import (
    get_pending_documents,
    legal_status,
)
from legal.services.acceptance_service import record_acceptance
from utils.request_meta import client_ip, client_user_agent
from guardians.permissions import HasGuardianConsentIfMinor
from legal.permissions import HasAcceptedCurrentTerms
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
from accounts.constants import normalize_country
from accounts.services.age_service import (
    AgeGateError,
    parse_birthdate,
    resolve_country,
    validate_signup_age,
)
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
    permission_classes = [
        IsAuthenticated, HasAcceptedCurrentTerms, HasGuardianConsentIfMinor
    ]

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

            # Rides along on the call the client already makes at every session
            # start, rather than a second request the gate would have to wait
            # for. Costs nothing: get_pending_documents reads the denormalized
            # columns on the user already loaded above and queries nothing.
            data["legal"] = legal_status(user)

            # Same idea as the block above, one gate along: a minor waiting on
            # a parent needs their waiting screen on the session-start call, not
            # behind a second request. Free for everybody else — guardian_status
            # only queries when the status is `pending` (see the selector).
            data["guardian"] = guardian_status(user)

            return response_data(success=True, data=data)
        
        except Exception as e:
            return response_data(
                success=False,
                error=f"failed to get user : {str(e)}",
                status_code=500
            )
        


class CheckUsernameAvailabilityAPIView(APIView):
    permission_classes = [
        IsAuthenticated, HasAcceptedCurrentTerms, HasGuardianConsentIfMinor
    ]

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
    permission_classes = [
        IsAuthenticated, HasAcceptedCurrentTerms, HasGuardianConsentIfMinor
    ]

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
    permission_classes = [
        IsAuthenticated, HasAcceptedCurrentTerms, HasGuardianConsentIfMinor
    ]

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

            # Read BEFORE the loop below overwrites it — the counter increment
            # underneath depends on knowing what was there.
            birthdate_before = profile.birthdate

            for field in profile_mapping:
                if field in data:
                    old_value = getattr(profile, field)
                    new_value = data[field]

                    setattr(profile, field, new_value)
                    profile_fields.append(field)

                    logger.debug(
                        f"{TAG} {field}: {old_value} → {new_value}"
                    )

            # A birthdate that ACTUALLY CHANGED spends the single self-serve
            # correction (see UpdateUserProfileSerializer.validate_birthdate,
            # which reads this counter and refuses the next one). Counted here
            # rather than in the serializer because a serializer that validates
            # must not have side effects: is_valid() runs before anything is
            # saved and can be followed by a rollback, and a counter
            # incremented by a change that never landed would lock the user out
            # of a correction they never made.
            #
            # The equality check is not redundant with the serializer's — that
            # one decides whether to ALLOW, this one decides whether to CHARGE,
            # and a re-save of the same date must do neither.
            if "birthdate" in data and data["birthdate"] != birthdate_before:
                user.birthdate_corrections += 1
                user_fields.append("birthdate_corrections")

                logger.info(
                    f"{TAG} Birthdate corrected user={user.id}, "
                    f"corrections={user.birthdate_corrections}"
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

    IT IS ALSO THE AGE GATE FOR GOOGLE SIGNUPS, and that is the part that is
    easy to miss. A Google account is created by GoogleAuthCallbackView without
    the user ever seeing the signup form — no password, no role, no consent and
    no date of birth are collected, because the only button they pressed was on
    Google's own screen. Every one of those gaps is closed here, at the one step
    a new Google user cannot skip. Leaving the age check to the signup form
    alone would mean the entire OAuth half of signups walked straight past it.
    """
    permission_classes = [
        IsAuthenticated, HasAcceptedCurrentTerms, HasGuardianConsentIfMinor
    ]

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

        # THE CONSENT STEP FOR GOOGLE SIGNUPS.
        #
        # An email signup accepted at the form and arrives here with nothing
        # pending, so this is a no-op for them. A Google user was created
        # without being asked anything, so this — the step they cannot skip —
        # is where the checkbox lives and where the agreement is filed.
        #
        # Required, not optional: the client showing a checkbox is a UI
        # promise, and a UI promise is not a consent record. If documents are
        # pending, the role does not get set without one.
        pending = get_pending_documents(user)
        if pending:
            if request.data.get("accepted_terms") is not True:
                logger.warning(f"{TAG} Consent missing user={user.id}")
                return response_data(
                    False,
                    "You must accept the terms and privacy policy",
                    status_code=400,
                )

            record_acceptance(
                user=user,
                documents=pending,
                ip_address=client_ip(request),
                user_agent=client_user_agent(request),
            )
            logger.info(f"{TAG} Consent recorded user={user.id}")

        # THE AGE STEP FOR GOOGLE SIGNUPS — sited here, beside consent, on
        # purpose. Both are things the OAuth flow never asked, both are
        # preconditions of a usable account, and putting them at the same gate
        # means there is exactly one place to check rather than two that can
        # drift apart. A user cannot come out of this endpoint with a confirmed
        # role and no age on file.
        #
        # Conditional on the user not already having them, so this is a no-op
        # for an email signup passing through (it answered both at the form)
        # and for any Google user who has already been through it once — the
        # endpoint stays usable for a plain role change during onboarding.
        profile = getattr(user, "profile", None)
        needs_birthdate = profile is None or profile.birthdate is None
        needs_country = not user.country_code

        birthdate = None
        country_code = user.country_code

        if needs_birthdate or needs_country:
            if needs_birthdate and not request.data.get("birthdate"):
                logger.warning(f"{TAG} Birthdate missing user={user.id}")
                return response_data(
                    False, "Date of birth is required", status_code=400
                )

            if needs_country and not normalize_country(
                request.data.get("country_code")
            ):
                logger.warning(f"{TAG} Country missing user={user.id}")
                return response_data(
                    False, "A valid country is required", status_code=400
                )

            try:
                if needs_country:
                    # Same cross-check the signup form gets. user.phone is
                    # normally empty for a Google account, in which case
                    # resolve_country simply returns the declaration.
                    country_code = resolve_country(
                        request.data.get("country_code"), user.phone
                    )

                if needs_birthdate:
                    birthdate = parse_birthdate(request.data.get("birthdate"))
                else:
                    birthdate = profile.birthdate

                # The SAME validation and the SAME neutral rejection as the
                # email form. A Google account that fails it is refused a role,
                # which is what keeps it out of the app.
                validate_signup_age(birthdate, country_code)
            except AgeGateError as age_error:
                return response_data(
                    False,
                    age_error.detail[0] if age_error.detail else "",
                    {"code": age_error.error_code},
                    status_code=400,
                )

        # One transaction: the role, the jurisdiction and the birthdate are
        # written together or not at all, so there is no state in which the
        # role got confirmed but the age write failed.
        with transaction.atomic():
            user.role = role
            user.is_role_confirmed = True

            user_fields = ["role", "is_role_confirmed", "updated_at"]

            if needs_country:
                user.country_code = country_code
                user_fields.insert(2, "country_code")

            user.save(update_fields=user_fields)

            if needs_birthdate:
                if profile is None:
                    # Every path that creates a user creates its profile in the
                    # same transaction, so this should be unreachable — but the
                    # birthdate has already been validated by now and dropping
                    # it would leave a confirmed account whose age reads as
                    # unknown, which is the exact state this endpoint exists to
                    # prevent. Materialize the row rather than lose the answer.
                    profile = UserProfile.objects.create(
                        user=user, name=user.username or ""
                    )

                profile.birthdate = birthdate
                profile.save(update_fields=["birthdate", "updated_at"])

        logger.info(f"{TAG} Role set user={user.id}, role={role}")

        # THE MINOR LOCK FOR GOOGLE SIGNUPS — the twin of the one in
        # VerifySignupOTPAPIView, and here for the same reason the age gate
        # above is: this is the step a new Google user cannot skip, and the
        # first moment their birthdate is on file.
        #
        # Re-read first. The reverse one-to-one cached on `user` may hold the
        # "no profile" miss from the top of this method, and a stale missing
        # birthdate reads as a minor (accounts/constants.is_minor) — which
        # would lock an adult out behind a parent screen.
        user = User.objects.select_related("profile").get(pk=user.pk)
        guardian_required = ensure_pending_for_minor(user)

        data = UserSerializer(user).data
        # Alongside the user payload rather than inside it: this says what the
        # client should DO next, which is not a property of the account. See
        # the same key on the OTP verification response.
        data["guardian_required"] = guardian_required

        return response_data(
            success=True,
            message="Role updated successfully",
            data=data
        )


class CompleteOnboardingAPIView(APIView):
    """
    Marks the post-signup onboarding flow as finished for the current user.

    Idempotent: calling it again once onboarding is already complete still returns
    success. After this succeeds the user's role becomes permanently locked (see
    SetUserRoleAPIView).
    """
    permission_classes = [
        IsAuthenticated, HasAcceptedCurrentTerms, HasGuardianConsentIfMinor
    ]

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