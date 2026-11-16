"""
POST /app/events — the installed app telling us it was installed or opened.

Anonymous-friendly (PublicAPIView: AllowAny, but a signed-in caller's JWT is
still read, so the row can name who uses the device). Nothing in the response
is meant for a person: the client fires it and forgets it.

Throttling fires in DRF's ``initial()``, before ``post()`` runs, so the 429 is
put into the standard envelope in ``handle_exception`` — the same reason the
support views do it there.
"""

import logging

from django.db import IntegrityError
from rest_framework.exceptions import ParseError, Throttled

from apps.pwa.serializers.app_event_serializers import parse_app_event
from apps.pwa.services.app_install_service import AppInstallService
from apps.pwa.throttles import AppEventsThrottle
from core.views.base_views import PublicAPIView
from utils.response import response_data

logger = logging.getLogger(__name__)

INVALID = "Invalid app event."
THROTTLED = "Too many requests. Please try again later."


class AppEventsAPIView(PublicAPIView):
    """
    Body: ``{"device_id": "<uuid>", "event": "installed" | "opened",
    "platform", "browser", "source"?}``.

    200 ``{"ok": true}`` in ``data``. 400 ``{"code": "invalid"}`` for a
    device_id that is not a UUID or an unknown event; every other unknown value
    is bucketed, not refused (see the parser).
    """

    # PublicAPIView's default is the 60/min public READ budget.
    throttle_classes = [AppEventsThrottle]

    def post(self, request):
        TAG = "AppEventsAPIView.post"

        try:
            data = request.data
        except ParseError:
            data = None

        event = parse_app_event(data)
        if event is None:
            return response_data(
                success=False,
                message=INVALID,
                status_code=400,
                data={"code": "invalid"},
            )

        try:
            AppInstallService.record(event, user=request.user)

        except IntegrityError as e:
            # A duplicate is never the client's problem: the row it wanted
            # exists either way, and a 500 would only make it retry.
            logger.warning(f"{TAG} | IntegrityError | event={event.event} | {e}")

        except Exception as e:
            logger.error(f"{TAG} | Error | event={event.event} | {e}", exc_info=True)
            return response_data(
                success=False,
                message="Something went wrong",
                status_code=500,
                error=str(e),
            )

        return response_data(success=True, data={"ok": True})

    def handle_exception(self, exc):
        if isinstance(exc, Throttled):
            wait = int(exc.wait or 0)
            logger.warning(f"AppEventsAPIView | Throttled | retry_after={wait}")
            return response_data(
                success=False,
                message=THROTTLED,
                status_code=429,
                data={"code": "throttled", "retry_after": wait},
            )

        return super().handle_exception(exc)
