"""
HTTP entry points for user-initiated account deletion, mounted under /user/.

Thin by design: read the body, hand it to
``accounts.services.account_deletion_service``, shape the answer. Every rule —
which credential this account confirms with, the sole-owner guard, the session
kill, the deactivation — lives in the service.

Plain ``APIView`` with an explicit permission pair rather than ``BaseAPIView``,
matching ChangePasswordAPIView next door: deleting an account is something a
PERSON does, not something an actor does, so there is no org header to resolve
and no ``request.actor`` to want.
"""

import logging

from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from accounts.services.account_deletion_service import (
    confirm_account_deletion,
    initiate_account_deletion,
)
from accounts.throttles import AccountDeleteThrottle
from legal.permissions import HasAcceptedCurrentTerms
from utils.cookies import delete_refresh_key_cookie
from utils.errors import flatten_validation_error
from utils.response import response_data

logger = logging.getLogger(__name__)

# The one sentence the client shows after a successful confirm. Returned as
# both the envelope's message and data.detail: the frontend's shared error/
# toast helper reads `message`, and `detail` is what this endpoint's contract
# names.
DELETED_DETAIL = (
    "Your account has been deactivated and will be permanently deleted "
    "in 30 days."
)


def _validation_response(tag, exc):
    """A service ValidationError → the standard 400 envelope."""
    flat = flatten_validation_error(exc.detail)
    logger.warning(f"{tag} | Validation Error | {flat['message']}")
    return response_data(
        success=False,
        message=flat["message"],
        status_code=400,
        error=flat["message"],
        data={"errors": flat["errors"]},
    )


class AccountDeleteInitiateAPIView(APIView):
    """
    POST /user/account/delete/initiate

    Which credential confirms this account:
      200 {"method": "password"}
      200 {"method": "otp", "sent_to": "s*****b@gmail.com"}   (code mailed)
      400 — sole owner of an organization (message names them)
    """

    permission_classes = [IsAuthenticated, HasAcceptedCurrentTerms]
    throttle_classes = [AccountDeleteThrottle]
    throttle_scope = "account_delete"

    def post(self, request):
        TAG = "AccountDeleteInitiateAPIView"

        try:
            data = initiate_account_deletion(request.user)
        except ValidationError as e:
            return _validation_response(TAG, e)

        return response_data(success=True, data=data)


class AccountDeleteConfirmAPIView(APIView):
    """
    POST /user/account/delete/confirm

    Body is ``{"password": "..."}`` or ``{"otp": "..."}`` — whichever the
    initiate step named for this account. Anything else is rejected with the
    same generic message a wrong one gets.

    On success the account is deactivated, every session is blacklisted and the
    refresh cookie is cleared, so this device is signed out along with the rest.
    """

    permission_classes = [IsAuthenticated, HasAcceptedCurrentTerms]
    throttle_classes = [AccountDeleteThrottle]
    throttle_scope = "account_delete"

    def post(self, request):
        TAG = "AccountDeleteConfirmAPIView"

        try:
            confirm_account_deletion(
                request.user,
                password=request.data.get("password"),
                otp=request.data.get("otp"),
            )
        except ValidationError as e:
            return _validation_response(TAG, e)

        response = response_data(
            success=True,
            message=DELETED_DETAIL,
            data={"detail": DELETED_DETAIL},
        )
        # The access token stays valid for its remaining minutes, but SimpleJWT
        # refuses an inactive user, so it is already inert. Clearing the cookie
        # is what stops the client trying to refresh a blacklisted session.
        delete_refresh_key_cookie(response)
        return response
