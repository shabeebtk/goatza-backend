"""
The owner's CV settings, mounted at /user/cv/.

Thin by design: the "players only, acting as themselves" rule is NOT re-checked
here — CVService owns it and raises PermissionDenied, which ``_service_error``
turns into the standard 403 envelope. One rule, one place.
"""

import logging

from rest_framework.exceptions import (
    NotFound,
    PermissionDenied,
    ValidationError,
)

from core.views.base_views import BaseAPIView
from cv.serializers.cv_serializers import (
    CVSettingsSerializer,
    CVSettingsUpdateSerializer,
)
from cv.services.cv_services import CVService
from utils.errors import error_body, flatten_validation_error
from utils.response import response_data

logger = logging.getLogger(__name__)


def _service_error(tag, exc):
    """
    Map a service exception onto the standard envelope: ValidationError → 400,
    PermissionDenied → 403, NotFound → 404. The message the service wrote is
    what the client reads.
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


class CVSettingsAPIView(BaseAPIView):
    """
    GET / PATCH /user/cv/settings — the signed-in player's own row.

    Self only: there is no id in the URL, so nobody can read or flip anybody
    else's CV.
    """

    def get(self, request):
        TAG = "CVSettingsAPIView"
        try:
            settings = CVService.get_or_create_settings(request.actor)

            return response_data(
                success=True,
                data=CVSettingsSerializer(settings).data,
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

    def patch(self, request):
        TAG = "UpdateCVSettingsAPIView"
        try:
            # 403 before 400: a coach never gets told what was wrong with a
            # body they were not allowed to send. Same gate the service uses.
            CVService.require_player(request.actor)

            serializer = CVSettingsUpdateSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)

            settings = CVService.update_settings(
                request.actor,
                serializer.validated_data,
            )

            return response_data(
                success=True,
                message="CV settings updated",
                data=CVSettingsSerializer(settings).data,
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
