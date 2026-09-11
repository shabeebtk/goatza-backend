"""
The guardian gate.

A minor whose guardian has not approved the account may do NOTHING except the
handful of things on the list below: name a guardian, chase them, read their
own account, and get out. Everything else — reading a feed, opening a profile,
sending a message, posting — answers 403 until a parent says yes.

WHY THIS ONE BLOCKS READS AND ``HasAcceptedCurrentTerms`` DOES NOT

The two gates are the same shape and are deliberately not the same rule, so the
difference is worth stating before somebody "fixes" it. The terms gate protects
US: it exists so that nobody accumulates more content under an agreement they
have not signed, and a user reading their own feed while a version bump is
pending harms nobody. This gate protects the CHILD, and under the DPDP rules
reading IS processing — a feed is assembled from a child's behaviour, a message
is delivered to them, a profile view is their data being served to somebody
else. "Read everything, write nothing" would leave an unconsented child using
the product in every way that matters, which is the exact thing a guardian has
not yet agreed to.

The same reasoning is what makes ``withdrawn`` block as hard as ``pending``. A
parent who takes permission back has asked for the processing to STOP; a lock
that still served reads would be a lock that ignored them.

WHY THE EXEMPT LIST IS PATHS, NOT A VIEW FLAG

Same answer as legal/permissions.py, and the list is kept in the same shape on
purpose: "what can a locked child still do?" has to be answerable by reading
one block, not by grepping for a decorator across a dozen apps. It also means
adding this permission to a view is always safe — an exempt path returns True
no matter which view serves it.

Get the list wrong in the other direction and the failure is total, and worse
here than it is for terms: a child who cannot reach ``/guardian/details`` can
never ask a parent, so nothing will ever unlock them and no support agent can
fix it from their side either. The list errs toward letting things through.

WHAT IS NOT HERE: the parent's own endpoints. Everything under
``/guardian/consent/`` is AllowAny and has no user at all — a parent has no
account — so this permission never runs on them (see rule 1 in
``has_permission``). They are not exempt; they are somewhere else entirely.
"""

import logging

from rest_framework.exceptions import APIException
from rest_framework.permissions import BasePermission

from apps.accounts.models import User

logger = logging.getLogger(__name__)

# The machine-readable half of the 403. The client switches on this to raise
# the "waiting for a parent" screen instead of the generic "something went
# wrong" toast, so it is part of the API contract — renaming it breaks that
# screen silently, and the account it breaks it for is a locked one.
GUARDIAN_CONSENT_REQUIRED_CODE = "GUARDIAN_CONSENT_REQUIRED"

GUARDIAN_CONSENT_REQUIRED_MESSAGE = (
    "A parent or guardian needs to approve this account first."
)

# The two statuses that lock an account. The other two — ``not_needed`` (every
# adult, and every minor nobody has had to assess) and ``approved`` — pass.
#
# Read as a SET rather than as "anything that is not approved", so that a value
# added to the column later (say a ``review`` state) has to be classified here
# by hand instead of silently locking every account that lands in it.
BLOCKING_STATUSES = frozenset({
    User.GuardianConsentStatus.PENDING,
    User.GuardianConsentStatus.WITHDRAWN,
})

# Paths a locked child must still be able to reach. Written without the
# trailing slash, like every route in this project (see core/urls.py).
EXEMPT_PATHS = frozenset({
    # THE WAY OUT. If nothing else on this list is right, these three must be:
    # naming a guardian is the only action that can ever clear this gate, and
    # resend is what a child does when the first email went nowhere.
    "/guardian/details",
    "/guardian/shared/approve",
    "/guardian/resend",

    # The session. A locked child who cannot refresh a token is a child whose
    # app breaks instead of showing them the parent screen, and one who cannot
    # log out is locked into the account rather than out of it.
    "/user/logout",
    "/user/token/refresh",

    # The client reads this at every session start, and the `guardian` block it
    # returns — status plus the masked parent contact — IS the waiting screen.
    # Gating it would make the lock invisible: the client would have no way to
    # tell "locked" from "broken".
    "/user/details",

    # The OTHER gate's way out, and it has to stay open here for the same
    # reason it is open there. A minor who is both locked and behind a terms
    # bump would otherwise have two gates each pointing at an endpoint the
    # other one blocks, which is a lockout no support agent can undo.
    "/legal/accept",
    "/legal/versions",
})


class GuardianConsentRequired(APIException):
    """
    403 with a body the client can branch on.

    Raised rather than returned as ``False`` for the same reason
    ``TermsAcceptanceRequired`` is: DRF's own denial path renders
    ``{"detail": "..."}`` and nothing else, and a client that has to
    pattern-match an English sentence is a client that breaks the first time
    somebody edits the copy.

    ``guardian_consent_status`` rides along because the two blocking states
    need different screens. "Waiting for your parent to approve" and "your
    parent removed permission" are not the same message to a 15-year-old, and
    the client cannot tell them apart from the code alone.
    """

    status_code = 403
    default_code = GUARDIAN_CONSENT_REQUIRED_CODE

    def __init__(self, guardian_consent_status):
        super().__init__({
            "detail": GUARDIAN_CONSENT_REQUIRED_MESSAGE,
            "code": GUARDIAN_CONSENT_REQUIRED_CODE,
            "guardian_consent_status": guardian_consent_status,
        })


class HasGuardianConsentIfMinor(BasePermission):
    """
    Blocks every request from a signed-in account whose guardian consent is
    pending or withdrawn, except on an exempt path.

    Three ways through, checked in this order:

      1. No authenticated user. Not this permission's problem; IsAuthenticated
         answers it, and answering here would turn an anonymous 401 into a
         confusing 403 about somebody's parent.
      2. A status that does not lock. This is the fast path and it is the one
         almost every request in the product takes — every adult account is
         ``not_needed``.
      3. An exempt path. See EXEMPT_PATHS above.

    Note the ORDER of 2 and 3 versus legal/permissions.py, which checks its
    exemptions first: there the cheap test is the method, here it is the
    status, and a single field compare is what the overwhelming majority of
    requests should cost.

    COSTS NO QUERIES. ``guardian_consent_status`` is a denormalized column on
    the user the authentication layer has already loaded — that is the entire
    reason the column exists rather than the gate walking
    ``GuardianConsentEvent`` (see guardians/models.py).
    """

    def has_permission(self, request, view):
        user = getattr(request, "user", None)
        if not user or not user.is_authenticated:
            return True

        # Defaulted rather than accessed bare: an object standing in for a user
        # without this column is not a locked child, and a gate that raised
        # AttributeError would fail closed on every request in a way nobody
        # could unlock.
        status = getattr(
            user,
            "guardian_consent_status",
            User.GuardianConsentStatus.NOT_NEEDED,
        )

        if status not in BLOCKING_STATUSES:
            return True

        if self._is_exempt(request):
            return True

        logger.info(
            f"[GUARDIAN GATE] Blocked | user={user.id} | "
            f"{request.method} {request.path} | status={status}"
        )
        raise GuardianConsentRequired(status)

    @staticmethod
    def _is_exempt(request) -> bool:
        # Normalized because a client that appends a slash is still asking for
        # the same endpoint, and Django's APPEND_SLASH would have redirected it
        # there anyway. Missing an exemption over one character is not a
        # trade-off worth having.
        path = (request.path or "").rstrip("/") or "/"
        return path in EXEMPT_PATHS
