"""
The child's side of parental consent — three POSTs, all thin.

WHY THESE DROP ``HasAcceptedCurrentTerms``

Every other authenticated view in the project takes the project default
(``IsAuthenticated`` + the terms gate). These three take ``IsAuthenticated``
alone, on purpose. They are the way OUT of a locked account: a pending minor
has to be able to name a guardian even when everything else about their account
is blocked, and a terms-version bump landing while a child waits for their
parent would otherwise leave them unable to reach either gate's exit. Same
reasoning that puts ``/legal/accept`` in that permission's own EXEMPT_PATHS.

They are still authenticated — the caller is the CHILD, always
``request.user``, never an id from the body. A child can only ever start
consent for themselves, which is what makes it safe for these to be open while
the account is locked. The parent's side of the exchange is a different
surface entirely (they have no account and answer through a token link), and
none of it is here.

Logic stays in ``guardians.services.consent_service``. What these views own is
the HTTP shape: which body key means what, and the two-value ``mode`` the
client branches on.
"""

import logging

from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from apps.guardians.constants import GuardianConsentMethod
from apps.guardians.selectors.consent_selectors import mask_contact
from apps.guardians.services.consent_service import (
    GuardianConsentError,
    record_shared_contact_approval,
    request_consent,
    resend,
)
from apps.guardians.throttles import GuardianRequestThrottle, GuardianResendThrottle
from utils.response import response_data

logger = logging.getLogger(__name__)

# The two values the client branches on. Deliberately NOT the service's method
# names: the service is recording HOW an approval was obtained, this is telling
# a client WHICH screen to show next — "we've emailed your parent, wait" versus
# "hand your phone over now".
MODE_LINK_SENT = "link_sent"
MODE_SHARED_CONTACT = "shared_contact"

CONTACT_REQUIRED_MESSAGE = (
    "Enter your parent or guardian's email address or phone number."
)

BOTH_CONTACTS_MESSAGE = (
    "Give one contact for your parent or guardian, not two."
)

# There is no SMS sender in this codebase (see consent_service.request_consent).
# Refused HERE, before the service writes anything, rather than recording a
# request nobody could ever deliver and leaving the child pending against a
# parent who will never hear about it.
PHONE_UNSUPPORTED_MESSAGE = (
    "We can only send permission requests by email right now. Enter your "
    "parent or guardian's email address."
)

CONFIRM_18_MESSAGE = (
    "Your parent or guardian must confirm they are 18 or older."
)


def _error(exc):
    """A GuardianConsentError as this project's 400 envelope, code and all."""
    return response_data(
        False,
        exc.detail[0] if exc.detail else "",
        {"code": exc.error_code},
        status_code=400,
    )


class GuardianDetailsAPIView(APIView):
    """
    POST /guardian/details — name a parent and start the exchange.

    Body: ``parent_name`` plus exactly one of ``parent_email`` /
    ``parent_phone``.

    Returns ``{"mode": "link_sent"|"shared_contact", "masked_contact": ...}``.
    ``shared_contact`` means the contact given is the child's OWN login email
    or phone, so no link was sent and nothing could be — the client's next step
    is the hand-the-phone screen, not a waiting screen. The service decides
    which of the two it is; this view only renames the answer.
    """

    permission_classes = [IsAuthenticated]
    throttle_classes = [GuardianRequestThrottle]
    throttle_scope = "guardian_request"

    def post(self, request):
        parent_name = request.data.get("parent_name")
        parent_email = request.data.get("parent_email")
        parent_phone = request.data.get("parent_phone")

        if parent_email and parent_phone:
            return response_data(False, BOTH_CONTACTS_MESSAGE, status_code=400)

        if not parent_email and not parent_phone:
            return response_data(False, CONTACT_REQUIRED_MESSAGE, status_code=400)

        if parent_phone:
            return response_data(
                False,
                PHONE_UNSUPPORTED_MESSAGE,
                {"code": "phone_not_supported"},
                status_code=400,
            )

        try:
            result = request_consent(
                child=request.user,
                parent_name=parent_name,
                parent_contact=parent_email,
                request=request,
            )
        except GuardianConsentError as exc:
            logger.warning(
                f"[GUARDIAN DETAILS] Rejected | user={request.user.id} | "
                f"code={exc.error_code}"
            )
            return _error(exc)

        shared = result["mode"] == GuardianConsentMethod.SHARED_CONTACT
        guardian = result["guardian"]

        return response_data(
            True,
            message=(
                "Ask your parent or guardian to approve on this device"
                if shared
                else "We've emailed your parent or guardian"
            ),
            data={
                "mode": MODE_SHARED_CONTACT if shared else MODE_LINK_SENT,
                # Masked even though the child typed it seconds ago: this is
                # the same string the waiting screen re-reads from
                # /user/details later, and two spellings of one value is how a
                # UI ends up showing the full address on one screen and the
                # masked one on the next.
                "masked_contact": mask_contact(guardian.email or guardian.phone),
            },
        )


class GuardianSharedApproveAPIView(APIView):
    """
    POST /guardian/shared/approve — the hand-the-phone approval.

    Body: ``parent_name``, ``confirm_18_plus`` (must be strictly True), and an
    optional ``parent_birthdate``.

    ``confirm_18_plus`` is checked here rather than in the service because it
    is a UI affordance — a checkbox on the screen the parent is looking at —
    and what the SERVICE records is the approval itself. Strictly True, the
    same rule ``accepted_terms`` gets at signup: a missing key, "", "false" and
    0 are all not a confirmation.

    The service refuses this route when the named guardian is reachable
    separately, so a child who already emailed a real parent cannot approve
    themselves here.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        if request.data.get("confirm_18_plus") is not True:
            return response_data(
                False,
                CONFIRM_18_MESSAGE,
                {"code": "confirm_18_plus_required"},
                status_code=400,
            )

        try:
            event = record_shared_contact_approval(
                child=request.user,
                parent_name=request.data.get("parent_name"),
                parent_birthdate=request.data.get("parent_birthdate") or None,
                request=request,
            )
        except GuardianConsentError as exc:
            logger.warning(
                f"[GUARDIAN SHARED APPROVE] Rejected | user={request.user.id} "
                f"| code={exc.error_code}"
            )
            return _error(exc)

        return response_data(
            True,
            message="Permission recorded",
            data={
                # Read back off the user the service just wrote, so the client
                # never has to assume what the write did.
                "guardian_consent_status": request.user.guardian_consent_status,
                "approved_at": event.created_at,
            },
        )


class GuardianResendAPIView(APIView):
    """
    POST /guardian/resend — send the link again, on a fresh token.

    No body. The guardian and the channel come from the standing request, not
    from the client: a "resend" that could name a different address would be
    ``/guardian/details`` with a rate limit somebody forgot to apply.

    Throttled hard — see ``GuardianResendThrottle``. The person on the other
    end has no account and no way to unsubscribe.
    """

    permission_classes = [IsAuthenticated]
    throttle_classes = [GuardianResendThrottle]
    throttle_scope = "guardian_resend"

    def post(self, request):
        try:
            result = resend(child=request.user, request=request)
        except GuardianConsentError as exc:
            logger.warning(
                f"[GUARDIAN RESEND] Rejected | user={request.user.id} | "
                f"code={exc.error_code}"
            )
            return _error(exc)

        guardian = result["guardian"]

        return response_data(
            True,
            message="We've emailed your parent or guardian again",
            data={
                "mode": MODE_LINK_SENT,
                "masked_contact": mask_contact(guardian.email or guardian.phone),
            },
        )
