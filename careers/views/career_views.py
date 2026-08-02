"""
HTTP entry points for career entries, mounted at /careers/.

Thin by design: resolve what the URL names, hand the actor and the validated
body to CareerEntryService / the selectors, and shape the answer. The "acting as
yourself, on your own entries" rule is NOT re-checked here — the service owns it
and raises PermissionDenied, which ``_service_error`` turns into the standard
403 envelope. One rule, one place.
"""

import logging

from rest_framework.exceptions import (
    NotFound,
    PermissionDenied,
    ValidationError,
)

from accounts.models import User
from careers.selectors.career_selectors import career_entries_for
from careers.serializers.career_serializers import (
    CareerEntryCreateSerializer,
    CareerEntrySerializer,
    CareerEntryUpdateSerializer,
    CareerFromApplicationSerializer,
)
from careers.services.career_services import CareerEntryService
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


class UserCareerEntryListAPIView(BaseAPIView):
    """
    GET /careers/users/<user_id> — that user's career history.

    Public to any authenticated actor, org actors included: a career is the
    part of a profile recruiters are meant to read, so there is no visibility
    filter and no owner-only fields to strip.
    """

    def get(self, request, user_id):
        TAG = "UserCareerEntryListAPIView"
        try:
            owner = User.objects.filter(id=user_id).first()

            if owner is None:
                return response_data(
                    success=False,
                    message="User not found",
                    status_code=404
                )

            entries = list(career_entries_for(owner))

            return response_data(
                success=True,
                data={
                    "count": len(entries),
                    "is_owner": bool(
                        request.actor
                        and request.actor.is_user
                        and request.actor.user
                        and request.actor.user.id == owner.id
                    ),
                    "results": CareerEntrySerializer(entries, many=True).data,
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


class CreateCareerEntryAPIView(BaseAPIView):
    """POST /careers/create — add a stint to your own history."""

    def post(self, request):
        TAG = "CreateCareerEntryAPIView"
        try:
            # 403 before 400: an org actor never gets told what was wrong with
            # a body they were not allowed to send. Same gate the service uses.
            CareerEntryService.require_user(request.actor)

            serializer = CareerEntryCreateSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)

            entry = CareerEntryService.create_entry(
                request.actor,
                payload=serializer.validated_data
            )

            logger.info(f"{TAG} | Career entry created | entry_id={entry.id}")

            # Re-read through the selector so the response carries the same
            # nested organization/sport/positions a list row would.
            entry = career_entries_for(entry.user).filter(id=entry.id).first()

            return response_data(
                success=True,
                message="Career entry added",
                status_code=201,
                data=CareerEntrySerializer(entry).data,
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


class CreateCareerEntryFromApplicationAPIView(BaseAPIView):
    """
    POST /careers/from-application/<application_id> — turn a selection into a
    career entry.

    Idempotent: an application that already has an entry answers 200 with that
    entry, a fresh one answers 201. The client can therefore fire this from the
    "add to career" prompt without tracking whether it already did.
    """

    def post(self, request, application_id):
        TAG = "CreateCareerEntryFromApplicationAPIView"
        try:
            CareerEntryService.require_user(request.actor)

            serializer = CareerFromApplicationSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)

            entry, created = CareerEntryService.create_from_application(
                request.actor,
                application_id,
                payload=serializer.validated_data,
            )

            if created:
                logger.info(
                    f"{TAG} | Career entry created from application | "
                    f"entry_id={entry.id} | application_id={application_id}"
                )

            entry = career_entries_for(entry.user).filter(id=entry.id).first()

            return response_data(
                success=True,
                message=(
                    "Career entry added"
                    if created
                    else "This is already on your career"
                ),
                status_code=201 if created else 200,
                data=CareerEntrySerializer(entry).data,
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


class CareerEntryDetailAPIView(BaseAPIView):
    """PATCH / DELETE /careers/<entry_id> — owner only."""

    def patch(self, request, entry_id):
        TAG = "UpdateCareerEntryAPIView"
        try:
            CareerEntryService.require_user(request.actor)

            serializer = CareerEntryUpdateSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)

            entry = CareerEntryService.update_entry(
                request.actor,
                entry_id,
                payload=serializer.validated_data
            )

            entry = career_entries_for(entry.user).filter(id=entry.id).first()

            return response_data(
                success=True,
                message="Career entry updated",
                data=CareerEntrySerializer(entry).data,
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

    def delete(self, request, entry_id):
        TAG = "DeleteCareerEntryAPIView"
        try:
            deleted_id = CareerEntryService.delete_entry(
                request.actor,
                entry_id
            )

            logger.info(f"{TAG} | Career entry deleted | entry_id={deleted_id}")

            return response_data(
                success=True,
                message="Career entry deleted",
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
