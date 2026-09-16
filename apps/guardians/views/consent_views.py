"""
The child's side of parental consent — two POSTs, both thin.

WHY THESE DROP ``HasAcceptedCurrentTerms``

Every other authenticated view in the project takes the project default
(``IsAuthenticated`` + the terms gate). These two take ``IsAuthenticated``
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
none of it is here. There is no approval endpoint on this side at all: an
approval only ever arrives through the parent's link, whoever's inbox it went
to.

Logic stays in ``guardians.services.consent_service``. What these views own is
the HTTP shape: which body key means what, and the ``data`` block the waiting
screen renders from.
"""

import logging

from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from apps.guardians.selectors.consent_selectors import mask_contact
from apps.guardians.services.consent_service import (
    GuardianConsentError,
    request_consent,
    resend,
)
from apps.guardians.throttles import GuardianRequestThrottle, GuardianResendThrottle
from utils.response import response_data

logger = logging.getLogger(__name__)

# The one value ``mode`` takes now that every request goes out as a link. Kept
# on the wire rather than dropped: the client stores it as "a parent has been
# named, go to the waiting screen", and a key that is always the same string is
# cheaper than a client release that has to stop reading it.
MODE_LINK_SENT = "link_sent"

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


def _error(exc):
    """A GuardianConsentError as this project's 400 envelope, code and all."""
    return response_data(
        False,
        exc.detail[0] if exc.detail else "",
        {"code": exc.error_code},
        status_code=400,
    )


def _link_sent(result):
    """
    The ``data`` block both POSTs answer with, built once so the details reply
    and the resend reply cannot drift::

        {"mode": "link_sent", "masked_contact": "p***a@gmail.com",
         "same_as_login_contact": false}

    ``same_as_login_contact`` is the service's own answer, passed through: the
    waiting screen uses it to say "the address you signed up with" rather than
    implying a second inbox exists.
    """
    guardian = result["guardian"]

    return {
        "mode": MODE_LINK_SENT,
        # Masked even though the child typed it seconds ago: this is the same
        # string the waiting screen re-reads from /user/details later, and two
        # spellings of one value is how a UI ends up showing the full address
        # on one screen and the masked one on the next.
        "masked_contact": mask_contact(guardian.email or guardian.phone),
        "same_as_login_contact": result["same_as_login_contact"],
    }


class GuardianDetailsAPIView(APIView):
    """
    POST /guardian/details — name a parent and start the exchange.

    Body: ``parent_name`` plus exactly one of ``parent_email`` /
    ``parent_phone``.

    Returns ``{"mode": "link_sent", "masked_contact": ..., "same_as_login_contact": ...}``
    — see ``_link_sent``. The link is sent whatever the address, the child's
    own login email included; the service records that case as the weaker
    approval it is when the parent answers, and this view only reports it.
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

        return response_data(
            True,
            message="We've emailed your parent or guardian",
            data=_link_sent(result),
        )


class GuardianResendAPIView(APIView):
    """
    POST /guardian/resend — send the link again, on a fresh token.

    No body. The guardian and the channel come from the standing request, not
    from the client: a "resend" that could name a different address would be
    ``/guardian/details`` with a rate limit somebody forgot to apply.

    Answers 400 ``consent_declined`` when the parent has already said no to
    the standing request — the client's next step is a new request, not this.

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

        return response_data(
            True,
            message="We've emailed your parent or guardian again",
            data=_link_sent(result),
        )
