"""
HTTP entry points for changing the login email, mounted under /user/.

Thin by design, exactly like account_deletion_views next door: read the body,
hand it to ``accounts.services.email_change_service``, shape the answer. Every
rule — the password gate, the Google-account refusal, the pending binding, the
notice to the old address — lives in the service.

Plain ``APIView`` with an explicit permission pair rather than ``BaseAPIView``:
an email address belongs to a PERSON, not to an actor, so there is no org
header to resolve and no ``request.actor`` to want.
"""

import logging

from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from accounts.services.email_change_service import (
    confirm_email_change,
    initiate_email_change,
)
from accounts.throttles import EmailChangeThrottle
from legal.permissions import HasAcceptedCurrentTerms
from utils.errors import flatten_validation_error
from utils.response import response_data

logger = logging.getLogger(__name__)

CHANGED_DETAIL = "Your email address has been updated."


def _validation_response(tag, exc):
    """
    A service ValidationError → the standard 400 envelope.

    Same shape as the deletion views', plus ``code`` when the service set one
    (EmailChangeError). The client needs it: "set a password first" is the one
    refusal that has to point somewhere else, and matching on the sentence
    would freeze the copy.
    """
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


class EmailChangeInitiateAPIView(APIView):
    """
    POST /user/email/change/initiate

    Body: ``{"new_email": "...", "password": "..."}``.

      200 {"sent_to": "n*****w@gmail.com", "expires_in": 600}
      400 {"code": "password_not_set"}   — Google-only account, no password yet
      400 {"code": "invalid_password"}
      400 {"code": "invalid_email" | "same_email" | "email_taken"}

    Nothing is written by this call; the address is only bound server-side
    until the code sent to it comes back.
    """

    permission_classes = [IsAuthenticated, HasAcceptedCurrentTerms]
    throttle_classes = [EmailChangeThrottle]
    throttle_scope = "email_change"

    def post(self, request):
        TAG = "EmailChangeInitiateAPIView"

        try:
            data = initiate_email_change(
                request.user,
                new_email=request.data.get("new_email"),
                password=request.data.get("password"),
            )
        except ValidationError as e:
            return _validation_response(TAG, e)

        return response_data(success=True, data=data)


class EmailChangeConfirmAPIView(APIView):
    """
    POST /user/email/change/confirm

    Body: ``{"otp": "..."}`` — the address is NOT accepted here, it comes from
    the binding initiate wrote.

      200 {"email": "new@example.com"}
      400 {"code": "invalid_code"}   — wrong, expired, or nothing pending
      400 {"code": "email_taken"}    — somebody claimed it between the steps

    The session is deliberately left alone: the caller just proved themselves
    with a password AND a mailed code, so their other devices stay signed in.
    """

    permission_classes = [IsAuthenticated, HasAcceptedCurrentTerms]
    throttle_classes = [EmailChangeThrottle]
    throttle_scope = "email_change"

    def post(self, request):
        TAG = "EmailChangeConfirmAPIView"

        try:
            new_email = confirm_email_change(
                request.user, otp=request.data.get("otp")
            )
        except ValidationError as e:
            return _validation_response(TAG, e)

        return response_data(
            success=True,
            message=CHANGED_DETAIL,
            # The client's copy of the user is now stale — hand back the value
            # it needs to fix it rather than making it re-fetch the profile.
            data={"email": new_email, "detail": CHANGED_DETAIL},
        )
