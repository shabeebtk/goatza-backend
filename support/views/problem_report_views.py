"""
HTTP entry points for "report a problem" — one authenticated, one anonymous.

Thin like moderation's report view: validate, hand to ProblemReportService, map
the outcome onto the standard envelope. Nothing about a report is decided here.

Both views own the 429, because throttling fires in DRF's ``initial()`` — before
any of the view body runs — so a try/except in ``post()`` never sees a
``Throttled`` and the default handler would answer with a bare
``{"detail": ...}``, the one response shape no client parser in this app
expects. ``handle_exception`` is the only place to catch it.

The two answers are deliberately identical in shape and status: one key,
``reference``, and a 200. That is what makes the public route's honeypot
response indistinguishable from a real one.
"""

import logging

from rest_framework.exceptions import Throttled, ValidationError

from core.views.base_views import BaseAPIView, PublicAPIView
from support.serializers.problem_report_serializers import (
    ProblemReportCreateSerializer,
    PublicProblemReportCreateSerializer,
)
from support.services.problem_report_service import ProblemReportService
from support.throttles import ProblemReportThrottle, PublicProblemReportThrottle
from utils.errors import error_body, flatten_validation_error
from utils.request_meta import client_ip, client_user_agent
from utils.response import response_data

logger = logging.getLogger(__name__)

# What the reporter is told, on both routes. Nothing about the queue, the
# status or whether it looked like spam: a confirmation, not a status page.
THANKS = "Thanks — our team will look into this."

# The 429 wording. Shared so the two routes cannot drift into telling the same
# person two different things about the same limit.
THROTTLED = "Too many reports. Please try again later."


def _throttled_response(tag, exc):
    """A ``Throttled`` in the standard envelope, with the retry window."""
    wait = int(exc.wait or 0)

    logger.warning("%s | Throttled | retry_after=%s", tag, wait)

    return response_data(
        success=False,
        message=THROTTLED,
        status_code=429,
        data={
            **error_body(THROTTLED),
            "retry_after": wait,
        },
    )


class ProblemReportAPIView(BaseAPIView):
    """
    POST /support/problem-report

    Body: ``{"category", "description", "screenshots"?, "contact_email"?,
    "client_context"?}``.

    Answers ``{"reference": "GZ-7K4M2P"}`` and nothing else — the code a
    reporter quotes if they write in about it.
    """

    throttle_classes = [ProblemReportThrottle]

    def post(self, request):
        TAG = "ProblemReportAPIView.post"

        try:
            serializer = ProblemReportCreateSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)

            data = serializer.validated_data

            success, result = ProblemReportService.create(
                # BOTH, and they are not the same thing: the actor decides
                # acting_org, while reported_by is always the human — and an
                # org Actor carries user=None, so request.user has to come
                # along separately (see the service's module docstring).
                actor=request.actor,
                user=request.user,
                category=data["category"],
                description=data["description"],
                screenshots=data.get("screenshots"),
                contact_email=data.get("contact_email", ""),
                client_context=data.get("client_context"),
                ip_address=client_ip(request),
                user_agent=client_user_agent(request),
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
                message=THANKS,
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
        Keep the 429 in the standard envelope — see the module docstring.
        """
        if isinstance(exc, Throttled):
            return _throttled_response("ProblemReportAPIView", exc)

        return super().handle_exception(exc)


class PublicProblemReportAPIView(PublicAPIView):
    """
    POST /public/support/problem-report

    The logged-out report. Text and a return address, no screenshots.

    Same body shape and same success payload as the authenticated route, so the
    client renders one confirmation sheet either way.
    """

    # PublicAPIView sets this to PublicReadThrottle (60/min), which is a read
    # budget and absurd for a write — inheriting it silently would leave this
    # endpoint effectively open. Same override as PlayerSignupCreateAPIView.
    throttle_classes = [PublicProblemReportThrottle]

    def post(self, request):
        TAG = "PublicProblemReportAPIView.post"

        try:
            serializer = PublicProblemReportCreateSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)

            data = serializer.validated_data

            # The honeypot. Answer exactly as if it had worked — same status,
            # same key, a code generated by the same function real references
            # come from — and write nothing. A bot that can tell it was caught
            # is a bot whose author stops filling the field in.
            if data.pop("website", "").strip():
                logger.info(f"{TAG} | Honeypot tripped | nothing persisted")
                return response_data(
                    success=True,
                    message=THANKS,
                    data=ProblemReportService.decoy_payload(),
                )

            success, result = ProblemReportService.create(
                # No actor and no user: this route is anonymous by definition,
                # so the report is filed with reported_by=None and the contact
                # email the serializer insisted on is the only way back.
                category=data["category"],
                description=data["description"],
                contact_email=data["contact_email"],
                client_context=data.get("client_context"),
                ip_address=client_ip(request),
                user_agent=client_user_agent(request),
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
                message=THANKS,
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

        except Exception as e:
            logger.error(f"{TAG} | Error | {str(e)}")
            return response_data(
                success=False,
                message="Something went wrong",
                status_code=500,
                error=str(e),
            )

    def handle_exception(self, exc):
        """Keep the 429 in the standard envelope — see the module docstring."""
        if isinstance(exc, Throttled):
            return _throttled_response("PublicProblemReportAPIView", exc)

        return super().handle_exception(exc)
