"""
HTTP entry points for achievements, mounted at /achievements/.

Thin by design: resolve what the URL names, hand the actor and the validated
body to AchievementService / the selectors, and shape the answer. The "acting as
yourself, on your own achievements" rule is NOT re-checked here — the service
owns it and raises PermissionDenied, which ``_service_error`` turns into the
standard 403 envelope. One rule, one place.
"""

import logging

from rest_framework.exceptions import (
    NotFound,
    PermissionDenied,
    ValidationError,
)

from accounts.models import User
from achievements.selectors.achievement_selectors import get_by_id, list_for_user
from achievements.serializers.achievement_serializers import (
    AchievementCreateSerializer,
    AchievementSerializer,
    AchievementUpdateSerializer,
)
from achievements.services.achievement_services import AchievementService
from core.views.base_views import BaseAPIView
from utils.errors import error_body, flatten_validation_error
from utils.response import response_data

logger = logging.getLogger(__name__)


def _service_error(tag, exc):
    """
    Map a service/selector exception onto the standard response envelope:
    ValidationError → 400, PermissionDenied → 403, NotFound → 404. The message
    the service wrote is what the client reads.
    """
    if isinstance(exc, ValidationError):
        flat = flatten_validation_error(exc.detail)
        logger.warning(f"{tag} | Validation Error | {flat['message']}")
        return response_data(
            success=False,
            message=flat["message"],
            status_code=400,
            error=flat["message"],
            data={"errors": flat["errors"]},
        )

    message = str(exc.detail)

    if isinstance(exc, PermissionDenied):
        logger.warning(f"{tag} | Forbidden | {message}")
        return response_data(
            success=False,
            message=message,
            status_code=403,
            data=error_body(message),
        )

    logger.info(f"{tag} | Not found | {message}")
    return response_data(
        success=False,
        message=message,
        status_code=404,
        data=error_body(message),
    )


class UserAchievementListAPIView(BaseAPIView):
    """
    GET /achievements/users/<user_id> — that user's achievements.

    Public to any authenticated actor, org actors included: an achievement
    shelf is the part of a profile recruiters are meant to read, so there is no
    visibility filter and no owner-only fields to strip. Same stance the career
    list takes.
    """

    def get(self, request, user_id):
        TAG = "UserAchievementListAPIView"
        try:
            owner = User.objects.filter(id=user_id).first()

            if owner is None:
                return response_data(
                    success=False,
                    message="User not found",
                    status_code=404
                )

            achievements = list(list_for_user(owner))

            return response_data(
                success=True,
                data={
                    "count": len(achievements),
                    "is_owner": bool(
                        request.actor
                        and request.actor.is_user
                        and request.actor.user
                        and request.actor.user.id == owner.id
                    ),
                    "results": AchievementSerializer(
                        achievements, many=True
                    ).data,
                },
            )

        except Exception as e:
            logger.error(f"{TAG} | Error | {str(e)}")
            return response_data(
                success=False,
                message="Something went wrong",
                status_code=500,
                error=str(e)
            )


class CreateAchievementAPIView(BaseAPIView):
    """POST /achievements/create — add an award to your own profile."""

    def post(self, request):
        TAG = "CreateAchievementAPIView"
        try:
            # 403 before 400: an org actor never gets told what was wrong with
            # a body they were not allowed to send. Same gate the service uses.
            AchievementService.require_user(request.actor)

            serializer = AchievementCreateSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)

            achievement = AchievementService.create_achievement(
                request.actor,
                payload=serializer.validated_data
            )

            logger.info(
                f"{TAG} | Achievement created | achievement_id={achievement.id}"
            )

            # Re-read through the selector so the response carries the same
            # nested sport/issuer/career-entry a list row would.
            achievement = (
                list_for_user(achievement.user)
                .filter(id=achievement.id)
                .first()
            )

            return response_data(
                success=True,
                message="Achievement added",
                status_code=201,
                data=AchievementSerializer(achievement).data,
            )

        except (ValidationError, PermissionDenied, NotFound) as e:
            return _service_error(TAG, e)

        except Exception as e:
            logger.error(f"{TAG} | Error | {str(e)}")
            return response_data(
                success=False,
                message="Something went wrong",
                status_code=500,
                error=str(e)
            )


class AchievementDetailAPIView(BaseAPIView):
    """GET / PATCH / DELETE /achievements/<achievement_id>."""

    def get(self, request, achievement_id):
        """
        One achievement on its own — what a shared link or a notification deep
        link opens. Readable by any authenticated actor, matching the list:
        nothing here is hidden from the profile page it already appears on.
        """
        TAG = "AchievementDetailAPIView"
        try:
            achievement = get_by_id(achievement_id)

            if achievement is None:
                return response_data(
                    success=False,
                    message="Achievement not found",
                    status_code=404
                )

            return response_data(
                success=True,
                data=AchievementSerializer(achievement).data,
            )

        except Exception as e:
            logger.error(f"{TAG} | Error | {str(e)}")
            return response_data(
                success=False,
                message="Something went wrong",
                status_code=500,
                error=str(e)
            )

    def patch(self, request, achievement_id):
        TAG = "UpdateAchievementAPIView"
        try:
            AchievementService.require_user(request.actor)

            serializer = AchievementUpdateSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)

            achievement = AchievementService.update_achievement(
                request.actor,
                achievement_id,
                payload=serializer.validated_data
            )

            achievement = (
                list_for_user(achievement.user)
                .filter(id=achievement.id)
                .first()
            )

            return response_data(
                success=True,
                message="Achievement updated",
                data=AchievementSerializer(achievement).data,
            )

        except (ValidationError, PermissionDenied, NotFound) as e:
            return _service_error(TAG, e)

        except Exception as e:
            logger.error(f"{TAG} | Error | {str(e)}")
            return response_data(
                success=False,
                message="Something went wrong",
                status_code=500,
                error=str(e)
            )

    def delete(self, request, achievement_id):
        TAG = "DeleteAchievementAPIView"
        try:
            deleted_id = AchievementService.delete_achievement(
                request.actor,
                achievement_id
            )

            logger.info(
                f"{TAG} | Achievement deleted | achievement_id={deleted_id}"
            )

            return response_data(
                success=True,
                message="Achievement deleted",
                data={"id": str(deleted_id)}
            )

        except (ValidationError, PermissionDenied, NotFound) as e:
            return _service_error(TAG, e)

        except Exception as e:
            logger.error(f"{TAG} | Error | {str(e)}")
            return response_data(
                success=False,
                message="Something went wrong",
                status_code=500,
                error=str(e)
            )
