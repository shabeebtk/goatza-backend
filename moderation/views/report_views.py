"""
HTTP entry point for reporting, mounted under /moderation/.

Thin like the block views: resolve, hand to ReportService, map the outcome onto
the standard envelope. The one thing this file owns that they do not is the 429
— throttling fires in DRF's ``initial()``, before any of the view body runs, so
the only place to catch it and keep the response shape uniform is
``handle_exception``.
"""

import logging

from rest_framework.exceptions import (
    NotFound,
    PermissionDenied,
    Throttled,
    ValidationError,
)

from core.views.base_views import BaseAPIView
from moderation.serializers.report_serializers import ReportCreateSerializer
from moderation.services.report_services import ReportService
from moderation.throttles import ReportThrottle
from utils.errors import error_body, flatten_validation_error
from utils.response import response_data

logger = logging.getLogger(__name__)

# What the reporter is told. Identical wording either way, because the client
# shows one confirmation sheet and the distinction — that they had already
# reported this — is not information they asked for or need.
THANKS = "Thanks — our team will review this."
ALREADY = "Thanks — already received."


class ReportAPIView(BaseAPIView):
    """
    POST /moderation/report

    Body: ``{"target_type", "target_id", "category", "details"}``.

    Answers 200 for both a new report and a duplicate of an open one; the
    payload's ``already_reported`` is the only thing that differs. Nothing
    about the reported account is echoed back — the reporter gets a
    confirmation, not a receipt they could use to probe.
    """

    throttle_classes = [ReportThrottle]

    def post(self, request):
        TAG = "ReportAPIView.post"
        try:
            serializer = ReportCreateSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)

            success, result = ReportService.create(
                actor=request.actor,
                **serializer.to_service_kwargs(),
            )

            if not success:
                return response_data(
                    success=False,
                    message=result,
                    status_code=400,
                    data=error_body(result),
                )

            return response_data(
                success=True,
                message=ALREADY if result["already_reported"] else THANKS,
                data=result,
            )

        except ValidationError as e:
            flat = flatten_validation_error(e.detail)
            logger.warning(f"{TAG} | Validation Error | {flat['message']}")
            return response_data(
                success=False,
                message=flat["message"],
                status_code=400,
                error=flat["message"],
                data={"errors": flat["errors"]},
            )

        except PermissionDenied as e:
            message = str(e.detail)
            logger.warning(f"{TAG} | Forbidden | {message}")
            return response_data(
                success=False,
                message=message,
                status_code=403,
                data=error_body(message),
            )

        except NotFound as e:
            # Never logged with the target id: the whole point of the generic
            # string is that this path reveals nothing, and a log line that
            # names what was probed rebuilds the oracle in the log file.
            message = str(e.detail)
            logger.info(f"{TAG} | Not found")
            return response_data(
                success=False,
                message=message,
                status_code=404,
                data=error_body(message),
            )

        except Exception as e:
            logger.error(f"{TAG} | Error | {str(e)}")
            return response_data(
                success=False,
                message="Something went wrong",
                status_code=500,
                error=str(e),
            )

    def handle_exception(self, exc):
        """
        Keep the 429 in the standard envelope.

        DRF raises Throttled from ``initial()``, which runs before ``post()``,
        so the try/except above never sees it and the default handler would
        answer with a bare ``{"detail": ...}`` — the one response shape in this
        app that no client parser expects.
        """
        if isinstance(exc, Throttled):
            wait = int(exc.wait or 0)
            message = "Too many reports. Please try again later."

            logger.warning("ReportAPIView | Throttled | retry_after=%s", wait)

            return response_data(
                success=False,
                message=message,
                status_code=429,
                data={
                    **error_body(message),
                    "retry_after": wait,
                },
            )

        return super().handle_exception(exc)
