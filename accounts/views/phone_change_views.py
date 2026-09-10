"""
HTTP entry point for changing the phone number, mounted under /user/.

Thin, like its email twin: read the body, hand it to
``accounts.services.phone_change_service``, shape the answer. The unique-column
guard, the email-or-phone constraint and the deliberate
``is_phone_verified = False`` all live in the service.

Plain ``APIView`` with an explicit permission pair rather than ``BaseAPIView``:
a phone number belongs to a PERSON, so there is no org header to resolve.
"""

import logging

from rest_framework.exceptions import ValidationError
from rest_framework.permissions import SAFE_METHODS, IsAuthenticated
from rest_framework.settings import api_settings
from rest_framework.views import APIView

from accounts.services.phone_change_service import change_phone
from accounts.throttles import PhoneChangeThrottle
from guardians.permissions import HasGuardianConsentIfMinor
from legal.permissions import HasAcceptedCurrentTerms
from utils.errors import flatten_validation_error
from utils.response import response_data

logger = logging.getLogger(__name__)

SAVED_DETAIL = "Your phone number has been updated."
REMOVED_DETAIL = "Your phone number has been removed."


def _validation_response(tag, exc):
    """A service ValidationError → the standard 400 envelope, plus its code."""
    flat = flatten_validation_error(exc.detail)
    code = getattr(exc, "error_code", None)

    logger.warning(f"{tag} | Validation Error | code={code} | {flat['message']}")

    return response_data(
        success=False,
        message=flat["message"],
        status_code=400,
        error=flat["message"],
        data={"errors": flat["errors"], "code": code},
    )


class PhoneChangeAPIView(APIView):
    """
    POST /user/phone/change   — body ``{"phone": "+919876543210"}``, or
                                ``{"phone": null}`` to remove it.
    GET  /user/phone/change   — the value currently on file.

      200 {"phone": "...", "is_phone_verified": false}
      400 {"code": "invalid_phone" | "phone_taken" | "phone_required"}

    POST rather than PATCH, matching ``update/profile/data``'s neighbours on
    the same prefix.

    THE GET EXISTS FOR THE SETTINGS SCREEN. ``phone`` is deliberately absent
    from every user serializer — ``UserSerializer`` and ``UserFullSerializer``
    both render OTHER people's profiles, and a phone number is not something
    the app publishes. This endpoint is scoped to ``request.user``, so it
    answers "what is my number" without widening what any profile payload
    carries.
    """

    permission_classes = [
        IsAuthenticated, HasAcceptedCurrentTerms, HasGuardianConsentIfMinor
    ]
    throttle_classes = [PhoneChangeThrottle]
    throttle_scope = "phone_change"

    def get_throttles(self):
        """
        The 10/hour budget is for the WRITE only.

        ScopedRateThrottle does not care which method it is counting, so left
        alone it would charge the settings screen's prefill against the same
        ten — and opening the page eleven times would lock a user out of
        editing their own number. The read falls back to the project defaults
        (``user``: 100/min), which is what every other GET in the app runs on.
        """
        if self.request.method in SAFE_METHODS:
            return [throttle() for throttle in api_settings.DEFAULT_THROTTLE_CLASSES]

        return super().get_throttles()

    def get(self, request):
        user = request.user
        return response_data(
            success=True,
            data={
                "phone": user.phone,
                "is_phone_verified": user.is_phone_verified,
            },
        )

    def post(self, request):
        TAG = "PhoneChangeAPIView"

        try:
            data = change_phone(request.user, request.data.get("phone"))
        except ValidationError as e:
            return _validation_response(TAG, e)

        return response_data(
            success=True,
            message=SAVED_DETAIL if data["phone"] else REMOVED_DETAIL,
            data=data,
        )
