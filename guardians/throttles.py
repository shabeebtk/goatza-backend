"""
Throttles for the guardian surface — the child's two mailing endpoints, and the
parent's anonymous ones.

The parent's are the interesting pair. Everything under /guardian/consent/ is
AllowAny with a token for identity, which means the IP is the only thing there
is to count, and the thing being protected is not a resource but a SECRET: the
only way to attack a consent link is to try tokens at it. Both are set
explicitly on their views for the reason WaitlistSignupThrottle spells out —
PublicAPIView hands down a 60/min read budget, and inheriting it silently is
how an endpoint ends up effectively open.
"""

from rest_framework.throttling import AnonRateThrottle, ScopedRateThrottle


class GuardianRequestThrottle(ScopedRateThrottle):
    """
    5/hour on ``POST /guardian/details`` (``guardian_request`` in
    DEFAULT_THROTTLE_RATES).

    The same spam-relay reasoning as ``EmailChangeThrottle``, and it applies
    harder here: this endpoint MAILS AN ADDRESS THE CALLER TYPED, with no
    verification step in between, and the mail it sends is a link. Left cheap
    it is a way to send Goatza-branded mail to anybody, which is worth more to
    an abuser than most of what the product does on purpose.

    Five is generous for the honest flow — one address, corrected once or twice
    when a parent says nothing arrived — and nothing legitimate needs a sixth
    in the same hour, because the retry for "no email came" is
    ``/guardian/resend``, which has a budget of its own.

    Keyed on the user, not the actor: the child's account is the thing being
    unlocked, and no org header should buy a second allowance.
    """

    scope = "guardian_request"


class GuardianResendThrottle(ScopedRateThrottle):
    """
    3/hour on ``POST /guardian/resend`` (``guardian_resend`` in
    DEFAULT_THROTTLE_RATES).

    Tighter than the request above, and the reason is the person on the other
    end. A resend goes to an address that has ALREADY been mailed and has
    already not answered — every extra one is a notification to somebody who
    did not ask to hear from us and cannot unsubscribe, because they have no
    account. Three is a child trying again after a parent says "I didn't get
    it", checking spam, and one more; past that the answer is not another email.

    It is also what keeps the token rules meaningful: every resend mints a new
    token and retires the old one (``consent_service.resend``), so an unlimited
    resend is an unlimited supply of live credentials for one child.

    Keyed on the user, like its neighbour.
    """

    scope = "guardian_resend"


class PublicConsentReadThrottle(AnonRateThrottle):
    """
    20/hour per IP on GET /guardian/consent/<token>
    (``guardian_consent_read`` in DEFAULT_THROTTLE_RATES).

    THIS IS A GUESSING LIMIT, not a load limit. The endpoint takes a secret in
    the URL and answers differently depending on whether it resolves, so an
    unlimited version is an oracle somebody can sit and turn. The token is 32
    bytes of ``secrets`` output and guessing one is not realistically possible
    at any rate — this is the belt to that braces, and it costs an honest
    parent nothing: opening the link, reloading the page and coming back
    tomorrow is three or four requests, not twenty.

    Per IP because there is nothing else. A parent has no account and no token
    of ours — the consent token identifies the LINK, not a person, so keying on
    it would let anyone with one link exhaust nothing but their own budget.

    Returns None for an authenticated caller like every AnonRateThrottle, who
    falls through to the 'user' bucket. That case is not the parent (they have
    no session) but a signed-in child opening their own link out of curiosity.
    """

    scope = "guardian_consent_read"


class PublicConsentWriteThrottle(AnonRateThrottle):
    """
    10/hour per IP on the three consent decisions
    (``guardian_consent_write`` in DEFAULT_THROTTLE_RATES).

    One budget across approve, decline and withdraw, in the same spirit as
    AccountDeleteThrottle's shared scope: they are three answers to one
    question, and a limit that gave each its own allowance would be a limit of
    thirty on the thing that matters.

    Tighter than the read because every one of these WRITES — an event row, a
    status change on a child's account, sometimes an email. Ten is far more
    than the honest flow needs (a parent answers once, maybe changes their mind
    later) and far less than a script wants.
    """

    scope = "guardian_consent_write"
