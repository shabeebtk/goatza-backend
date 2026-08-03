"""
HTTP entry points for the organization side of achievement verification,
mounted under /achievements/.

Thin by design, same as achievement_views: the org/role rule and the "must be
pending" rule both live in AchievementVerificationService and reach the client
through ``_service_error``.
"""

import logging

from rest_framework.exceptions import (
    NotFound,
    PermissionDenied,
    ValidationError,
)

from achievements.serializers.achievement_serializers import (
    AchievementRejectSerializer,
    AchievementVerificationRequestSerializer,
)
from achievements.services.achievement_verification_services import (
    AchievementVerificationService,
)
from achievements.views.achievement_views import _service_error
from core.views.base_views import BaseAPIView
from utils.errors import error_body
from utils.response import response_data

logger = logging.getLogger(__name__)


class AchievementVerificationRequestListAPIView(BaseAPIView):
    """
    GET /achievements/verification-requests — the acting org's review screen.

    ``?status=pending`` (default) is the work queue; ``?status=decided`` is the
    history of calls already made, which an org may revisit. Paginated with
    limit/offset like the career review list, so an org with a long history
    doesn't ship the whole thing on every open.
    """

    DEFAULT_LIMIT = 20
    MAX_LIMIT = 50

    def get(self, request):
        TAG = "AchievementVerificationRequestListAPIView"
        try:
            status = request.query_params.get("status", "pending")
            if status not in ("pending", "decided"):
                return response_data(
                    success=False,
                    message="status must be 'pending' or 'decided'.",
                    status_code=400,
                    data=error_body("status must be 'pending' or 'decided'."),
                )

            # Both list calls run require_reviewer themselves, so the 403 for a
            # COACH comes out of the service exactly as it does on the writes.
            queryset = (
                AchievementVerificationService.list_decided_for_org(request.actor)
                if status == "decided"
                else AchievementVerificationService.list_pending_for_org(request.actor)
            )

            limit, offset = self._page(request)

            # One COUNT for the tab badge, then one page. The count is taken
            # before slicing so "showing 20 of 63" is honest.
            total = queryset.count()
            achievements = list(queryset[offset:offset + limit])

            return response_data(
                success=True,
                data={
                    "count": total,
                    "limit": limit,
                    "offset": offset,
                    "has_more": offset + len(achievements) < total,
                    "status": status,
                    "results": AchievementVerificationRequestSerializer(
                        achievements,
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


class VerifyAchievementAPIView(BaseAPIView):
    """POST /achievements/<achievement_id>/verify — owner/admin confirms the claim."""

    def post(self, request, achievement_id):
        TAG = "VerifyAchievementAPIView"
        try:
            achievement = AchievementVerificationService.verify(
                request.actor,
                achievement_id
            )

            logger.info(
                f"{TAG} | Achievement verified | achievement_id={achievement.id}"
            )

            return response_data(
                success=True,
                message="Achievement verified",
                data=AchievementVerificationRequestSerializer(achievement).data,
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


class RejectAchievementAPIView(BaseAPIView):
    """POST /achievements/<achievement_id>/reject — owner/admin declines, with a note."""

    def post(self, request, achievement_id):
        TAG = "RejectAchievementAPIView"
        try:
            # 403 before 400: a COACH never gets told what was wrong with a body
            # they were not allowed to send.
            AchievementVerificationService.require_reviewer(request.actor)

            serializer = AchievementRejectSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)

            achievement = AchievementVerificationService.reject(
                request.actor,
                achievement_id,
                reason=serializer.validated_data.get("reason", "")
            )

            logger.info(
                f"{TAG} | Achievement rejected | achievement_id={achievement.id}"
            )

            return response_data(
                success=True,
                message="Achievement rejected",
                data=AchievementVerificationRequestSerializer(achievement).data,
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
