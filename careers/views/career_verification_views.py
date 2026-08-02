"""
HTTP entry points for the organization side of career verification, mounted
under /careers/.

Thin by design, same as career_views: the org/role rule and the "must be
pending" rule both live in CareerVerificationService and reach the client
through ``_service_error``.
"""

import logging

from rest_framework.exceptions import (
    NotFound,
    PermissionDenied,
    ValidationError,
)

from careers.selectors.career_selectors import (
    decided_verification_requests_for,
    pending_verification_requests_for,
)
from careers.serializers.career_serializers import (
    CareerEntrySerializer,
    CareerRejectSerializer,
    CareerVerificationRequestSerializer,
)
from careers.services.career_verification_services import CareerVerificationService
from careers.views.career_views import _service_error
from core.views.base_views import BaseAPIView
from utils.errors import error_body
from utils.response import response_data

logger = logging.getLogger(__name__)


class CareerVerificationRequestListAPIView(BaseAPIView):
    """
    GET /careers/verification-requests — the acting org's review screen.

    ``?status=pending`` (default) is the work queue; ``?status=decided`` is the
    history of calls already made, which a club may revisit. Paginated with
    limit/offset like the recruitment applicant list, so a club with a long
    history doesn't ship the whole thing on every open.
    """

    DEFAULT_LIMIT = 20
    MAX_LIMIT = 50

    def get(self, request):
        TAG = "CareerVerificationRequestListAPIView"
        try:
            organization, _ = CareerVerificationService.require_reviewer(
                request.actor
            )

            status = request.query_params.get("status", "pending")
            if status not in ("pending", "decided"):
                return response_data(
                    success=False,
                    message="status must be 'pending' or 'decided'.",
                    status_code=400,
                    data=error_body("status must be 'pending' or 'decided'."),
                )

            limit, offset = self._page(request)

            queryset = (
                decided_verification_requests_for(organization)
                if status == "decided"
                else pending_verification_requests_for(organization)
            )

            # One COUNT for the tab badge, then one page. The count is taken
            # before slicing so "showing 20 of 63" is honest.
            total = queryset.count()
            entries = list(queryset[offset:offset + limit])

            return response_data(
                success=True,
                data={
                    "count": total,
                    "limit": limit,
                    "offset": offset,
                    "has_more": offset + len(entries) < total,
                    "status": status,
                    "results": CareerVerificationRequestSerializer(
                        entries,
                        many=True
                    ).data,
                },
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

    def _page(self, request):
        """Clamp limit/offset so a client can't ask for the whole table."""
        def as_int(name, default):
            try:
                return int(request.query_params.get(name, default))
            except (TypeError, ValueError):
                return default

        limit = as_int("limit", self.DEFAULT_LIMIT)
        offset = as_int("offset", 0)

        limit = max(1, min(limit, self.MAX_LIMIT))
        offset = max(0, offset)

        return limit, offset


class VerifyCareerEntryAPIView(BaseAPIView):
    """POST /careers/<entry_id>/verify — owner/admin confirms the claim."""

    def post(self, request, entry_id):
        TAG = "VerifyCareerEntryAPIView"
        try:
            entry = CareerVerificationService.verify_entry(
                request.actor,
                entry_id
            )

            logger.info(f"{TAG} | Career entry verified | entry_id={entry.id}")

            return response_data(
                success=True,
                message="Career entry verified",
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


class RejectCareerEntryAPIView(BaseAPIView):
    """POST /careers/<entry_id>/reject — owner/admin declines, with a note."""

    def post(self, request, entry_id):
        TAG = "RejectCareerEntryAPIView"
        try:
            # 403 before 400: a COACH never gets told what was wrong with a body
            # they were not allowed to send.
            CareerVerificationService.require_reviewer(request.actor)

            serializer = CareerRejectSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)

            entry = CareerVerificationService.reject_entry(
                request.actor,
                entry_id,
                reason=serializer.validated_data.get("reason", "")
            )

            logger.info(f"{TAG} | Career entry rejected | entry_id={entry.id}")

            return response_data(
                success=True,
                message="Career entry rejected",
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
