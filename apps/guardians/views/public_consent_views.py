"""
The PARENT's side of consent — four endpoints, no login of any kind.

THE TOKEN IS THE ENTIRE IDENTITY. There is no session, no account, no password
and nothing to authenticate against: a caller here is a stranger holding a
string until that string resolves, and every one of these views is written on
that assumption.

Two rules follow, and they are the whole design of this module.

1. ONE REFUSAL, FOR EVERYTHING. A token that never existed, one that expired,
   one already answered, one retired by a resend and one belonging to an
   exchange that ended all produce the SAME body, the same message and the same
   404 — see ``_unresolved``. Not a courtesy: any difference between them is an
   oracle. "Expired" tells a stranger the token was real, which tells them
   these strings are guessable in principle and that this one hit; a distinct
   "already used" tells them a child on the other end has a parent who
   answered. So the only thing this surface ever says about a link it will not
   act on is that it will not act on it.

   The cost is real and is accepted: a parent whose link genuinely expired is
   told "this link is no longer valid" rather than "expired on the 3rd". The
   message names the way out — ask for a new one — which is the only thing they
   could act on anyway.

2. NOTHING ABOUT A CHILD LEAVES WITHOUT A RESOLVED TOKEN. The page payload is
   assembled by ``selectors.consent_page``, which returns None rather than an
   event when there is nothing a stranger may see, so a leak would have to be
   written deliberately rather than forgotten.

Validation of the BODY happens before the token is touched, on purpose. A
missing ``confirm_18_plus`` is answered the same way whether the token is real
or garbage, so the order of the checks cannot become the oracle the messages
were careful not to be.

ONE THING THIS DESIGN CANNOT HIDE: the token is in the URL, so it is in the
request line, so it is in every access log that records one — Django's own
``django.server``/``django.request`` loggers, and whatever sits in front of the
app in production. Nothing in this module can prevent that; a link sent to an
inbox has to carry its secret somewhere a browser will follow. What makes it
survivable is that the credential is short-lived and single-use: seven days
(``TOKEN_TTL_DAYS``), spent the moment it is answered, and retired by the next
resend. It is the same bargain every password-reset link on the internet makes.
The code below adds nothing to that exposure — it never writes the token into a
log line of its own — but it does not undo it either.
"""

import logging

from rest_framework.exceptions import Throttled

from core.views.base_views import PublicAPIView
from apps.guardians.selectors.consent_selectors import consent_page
from apps.guardians.services.consent_service import (
    GuardianConsentError,
    approve_by_token,
    decline_by_token,
    withdraw_by_token,
)
from apps.guardians.throttles import (
    PublicConsentReadThrottle,
    PublicConsentWriteThrottle,
)
from utils.errors import error_body
from utils.response import response_data

logger = logging.getLogger(__name__)

# THE one refusal. Covers unknown, expired, spent, superseded and finished
# links, and says nothing that distinguishes them.
UNRESOLVED_MESSAGE = (
    "This permission link is no longer valid. It may have expired or already "
    "been used — ask whoever sent it to send a new one."
)

# 404 rather than 400 or 403. A 400 would imply the input was malformed, a 403
# that something exists and is being withheld; 404 is the only one of the three
# that says nothing at all about whether there was ever anything here.
UNRESOLVED_STATUS = 404

CONFIRM_18_MESSAGE = (
    "Please confirm you are 18 or older and this child's parent or guardian."
)

PARENT_NAME_MESSAGE = "Please enter your full name."

THROTTLED_MESSAGE = "Too many attempts. Please try again later."

# What the POSTs report back. Deliberately not the GET's state vocabulary: this
# says what just happened, not what a page should render.
RESULT_APPROVED = "approved"
RESULT_DECLINED = "declined"
RESULT_WITHDRAWN = "withdrawn"


def _unresolved(tag, token, reason):
    """
    The single refusal, logged with WHY on our side and never on theirs.

    ``reason`` is the service's error code (or a marker of our own). It goes to
    the log, which is the one place the difference between "expired" and "never
    existed" is safe and genuinely useful — somebody will eventually have to
    answer "my mum says the link doesn't work". Its length goes with it, since
    "42 characters" and "43 characters" is the difference between a truncated
    paste and a real token, which is the other half of that support question.

    THE TOKEN ITSELF IS NOT WRITTEN HERE. That is a rule about this line, not a
    guarantee about the process — the access log already has the whole URL (see
    the module docstring). It is still worth keeping: a credential should not be
    in two places when one of them is avoidable.
    """
    logger.info("%s | Unresolved token | reason=%s | len=%s", tag, reason, len(token or ""))

    return response_data(
        success=False,
        message=UNRESOLVED_MESSAGE,
        data=error_body(UNRESOLVED_MESSAGE),
        status_code=UNRESOLVED_STATUS,
    )


class PublicConsentBaseView(PublicAPIView):
    """
    Shared plumbing: the standard 429 envelope, and nothing else.

    Throttling fires in DRF's ``initial()``, before any view body runs, so a
    try/except inside the handler never sees it and the default renderer would
    answer a bare ``{"detail": ...}`` — the one shape no client parser in this
    app expects. Same override, same reason, as the support views.
    """

    def handle_exception(self, exc):
        if isinstance(exc, Throttled):
            wait = int(exc.wait or 0)

            logger.warning(
                "%s | Throttled | retry_after=%s", type(self).__name__, wait
            )

            return response_data(
                success=False,
                message=THROTTLED_MESSAGE,
                data={**error_body(THROTTLED_MESSAGE), "retry_after": wait},
                status_code=429,
            )

        return super().handle_exception(exc)


class PublicConsentPageAPIView(PublicConsentBaseView):
    """
    GET /guardian/consent/<token>

    What the parent's page renders: which child is asking, everything we would
    hold about them, the version of the notice being shown, any children of
    theirs already approved, and whether this link is awaiting an answer or has
    one.

    ``siblings`` is the reason a second child is not a second explanation — a
    parent who approved one already sees so, and it doubles as corroboration
    that this is really their family and not a stranger's link.
    """

    throttle_classes = [PublicConsentReadThrottle]

    def get(self, request, token):
        page = consent_page(token)

        if page is None:
            return _unresolved("PublicConsentPageAPIView", token, "no_page")

        return response_data(success=True, data=page)


class PublicConsentApproveAPIView(PublicConsentBaseView):
    """
    POST /guardian/consent/<token>/approve

    Body: ``parent_name`` (required), ``confirm_18_plus`` (strictly true),
    ``parent_birthdate`` (optional).

    Unlocks the child. ``confirm_18_plus`` is checked here rather than in the
    service for the same reason the child-side view checks it: it is an
    affordance on a screen, while what the service records is the approval
    itself. Strictly True — a missing key, "", "false" and 0 are not a
    confirmation, the same rule ``accepted_terms`` gets at signup.
    """

    throttle_classes = [PublicConsentWriteThrottle]

    def post(self, request, token):
        TAG = "PublicConsentApproveAPIView"

        parent_name = str(request.data.get("parent_name") or "").strip()

        if not parent_name:
            return response_data(
                success=False,
                message=PARENT_NAME_MESSAGE,
                data=error_body(PARENT_NAME_MESSAGE, "parent_name"),
                status_code=400,
            )

        if request.data.get("confirm_18_plus") is not True:
            return response_data(
                success=False,
                message=CONFIRM_18_MESSAGE,
                data=error_body(CONFIRM_18_MESSAGE, "confirm_18_plus"),
                status_code=400,
            )

        try:
            event = approve_by_token(
                raw_token=token,
                parent_name=parent_name,
                parent_birthdate=request.data.get("parent_birthdate") or None,
                request=request,
            )
        except GuardianConsentError as exc:
            return _unresolved(TAG, token, exc.error_code)

        logger.info("%s | Approved | child=%s", TAG, event.child_id)

        return response_data(
            success=True,
            message="Thank you — permission recorded.",
            data={
                "result": RESULT_APPROVED,
                # Safe to echo: the caller just proved they hold this link, and
                # it is the same handle the GET already showed them.
                "child_username": event.child.username or "",
                "notice_version": event.notice_version,
            },
        )


class PublicConsentDeclineAPIView(PublicConsentBaseView):
    """
    POST /guardian/consent/<token>/decline

    No body. The child STAYS LOCKED and stays pending — a decline ends this
    request, not the account. Very often it means the child typed the wrong
    address or named the parent who was never going to answer, and a terminal
    state here would leave a 15-year-old with a dead account and no way back.
    They can name a different guardian from their side and try again.
    """

    throttle_classes = [PublicConsentWriteThrottle]

    def post(self, request, token):
        TAG = "PublicConsentDeclineAPIView"

        try:
            event = decline_by_token(raw_token=token, request=request)
        except GuardianConsentError as exc:
            return _unresolved(TAG, token, exc.error_code)

        logger.info("%s | Declined | child=%s", TAG, event.child_id)

        return response_data(
            success=True,
            message="Thanks — we've recorded that you did not give permission.",
            data={"result": RESULT_DECLINED},
        )


class PublicConsentWithdrawAPIView(PublicConsentBaseView):
    """
    POST /guardian/consent/<token>/withdraw

    No body. Relocks the child — status ``withdrawn``, which is kept apart from
    ``pending`` so nothing chases a parent who has already answered.

    Works on a link whose consent was approved, however long ago: the service
    skips the expiry check on this path alone, because the email promises "you
    can remove your permission later from this same link" and a promise that
    lapses after seven days is not one.

    Idempotent — a parent who clicks twice gets the same answer, not an error
    and not a second revocation.
    """

    throttle_classes = [PublicConsentWriteThrottle]

    def post(self, request, token):
        TAG = "PublicConsentWithdrawAPIView"

        try:
            event = withdraw_by_token(raw_token=token, request=request)
        except GuardianConsentError as exc:
            return _unresolved(TAG, token, exc.error_code)

        logger.info("%s | Withdrawn | child=%s", TAG, event.child_id)

        return response_data(
            success=True,
            message="Your permission has been removed and the account is locked.",
            data={"result": RESULT_WITHDRAWN},
        )
