"""
The consent gate.

A user who has not accepted the CURRENT terms may read everything and write
nothing. That asymmetry is the whole design: they keep their account, their
feed, their messages and their profile, and the only thing that stops working
is adding to the pile until they agree.

WHY THE EXEMPT LIST IS PATHS, NOT A VIEW FLAG

Every recovery route is named here, in one readable block, for the same reason
core/public_urls.py exists: "what can a blocked user still do?" has to be
answerable by reading one list, not by grepping for a decorator across a dozen
apps. It also means adding this permission to a view is always safe — an exempt
path returns True no matter which view serves it — which is what makes it
possible to put the gate on BaseAPIView and be done.

Get the list wrong in the other direction and the failure is total: a user who
cannot reach legal/accept can never clear the gate, and nothing in the product
will work for them again. That is the one bug in this file that cannot be
fixed by the user, so the list errs toward letting things through.
"""

import logging

from rest_framework.exceptions import APIException
from rest_framework.permissions import SAFE_METHODS, BasePermission

from legal.selectors.acceptance_selectors import get_pending_documents

logger = logging.getLogger(__name__)

# The machine-readable half of the 403. The client switches on this to raise
# the re-consent modal instead of the generic "something went wrong" toast, so
# it is part of the API contract — renaming it breaks the modal silently.
TERMS_REQUIRED_CODE = "TERMS_ACCEPTANCE_REQUIRED"

TERMS_REQUIRED_MESSAGE = (
    "Please accept the updated terms and privacy policy to continue."
)

# Paths a blocked user must still be able to POST to. Written without the
# trailing slash, like every route in this project (see core/urls.py).
EXEMPT_PATHS = frozenset({
    # The way out. If nothing else on this list is right, these two must be.
    "/legal/accept",
    "/legal/versions",

    # Authentication. None of it is reachable as a signed-in, gated user
    # anyway — except logout and refresh, and a user who cannot log out or
    # refresh a token is a user whose session breaks instead of prompting.
    "/user/signup",
    "/user/verify/otp",
    "/user/login",
    "/user/logout",
    "/user/token/refresh",
    "/user/forgot/password",
    "/user/reset/password",
    "/user/auth/google/login/url",
    "/user/auth/google/callback",

    # The client reads this on every session start, and the `legal` block it
    # returns is how the client knows to show the modal at all. Gating it
    # would make the gate invisible.
    "/user/details",

    # A user part-way through onboarding still has to finish it. Both are
    # POSTs, and both would otherwise strand a new account between signup and
    # a usable profile.
    "/user/role",
    "/user/onboarding/complete",

    # THERE IS NO ACCOUNT-DELETION ENDPOINT YET. When one is added it belongs
    # here: "I do not accept the new terms" and "I want my account gone" are
    # the same sentence, and a gate that blocks the exit is the one refusal
    # that turns a consent prompt into a hostage situation.
})


class TermsAcceptanceRequired(APIException):
    """
    403 with a body the client can branch on.

    Raised rather than returned as ``False`` because DRF's own denial path
    renders ``{"detail": "..."}`` and nothing else — there is no hook for the
    extra keys. The client needs to know WHICH documents are pending to render
    the modal, and a string it has to pattern-match is not an API.

    Deliberately not the project's ``response_data`` envelope. This travels the
    DRF exception path, which every other 4xx from a permission or a throttle
    also travels, and a body that is shaped like the envelope on some errors
    and not others is worse than one that is consistently DRF's.
    """

    status_code = 403
    default_code = TERMS_REQUIRED_CODE

    def __init__(self, pending_documents):
        super().__init__({
            "detail": TERMS_REQUIRED_MESSAGE,
            "code": TERMS_REQUIRED_CODE,
            "pending_documents": list(pending_documents),
        })


class HasAcceptedCurrentTerms(BasePermission):
    """
    Blocks unsafe methods for a signed-in user with a pending document.

    Three ways through, checked in this order:

      1. A safe method (GET/HEAD/OPTIONS). Reading is never gated — the user
         has to be able to read the documents they are being asked to accept,
         and punishing them by hiding their own account helps nobody.
      2. No authenticated user. Not this permission's problem; IsAuthenticated
         answers it, and answering here would turn every anonymous 401 into a
         confusing 403 about terms.
      3. An exempt path. See EXEMPT_PATHS above.

    Costs nothing on the hot path: get_pending_documents reads denormalized
    columns on the user object the authentication layer already loaded.
    """

    def has_permission(self, request, view):
        if request.method in SAFE_METHODS:
            return True

        user = getattr(request, "user", None)
        if not user or not user.is_authenticated:
            return True

        if self._is_exempt(request):
            return True

        pending = get_pending_documents(user)
        if not pending:
            return True

        logger.info(
            f"[LEGAL GATE] Blocked | user={user.id} | "
            f"{request.method} {request.path} | pending={','.join(pending)}"
        )
        raise TermsAcceptanceRequired(pending)

    @staticmethod
    def _is_exempt(request) -> bool:
        # Normalized because a client that appends a slash is still asking for
        # the same endpoint, and Django's APPEND_SLASH would have redirected it
        # there anyway. Missing an exemption over one character is not a
        # trade-off worth having.
        path = (request.path or "").rstrip("/") or "/"
        return path in EXEMPT_PATHS
