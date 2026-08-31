"""Throttle for the one write on the legal surface."""

from rest_framework.throttling import UserRateThrottle


class LegalAcceptThrottle(UserRateThrottle):
    """
    10/day on recording consent (``legal_accept`` in DEFAULT_THROTTLE_RATES).

    Per USER, like moderation's report throttle and for the same reason: the
    consent belongs to the human, not to whichever actor headers the request
    happens to carry.

    A day's budget is generous for what this endpoint is actually for. A user
    meets it once at signup — where the signup view records consent server-side
    and this endpoint is not involved at all — and after that only when a
    version is bumped, which is a handful of times a year. Ten leaves room for
    a flaky connection, a re-login on a second device and a genuine second
    document, and nothing legitimate needs the eleventh.

    Cheap to trip and harmless when it does: the acceptances already recorded
    stand, and re-recording one was a no-op anyway (get_or_create).
    """

    scope = "legal_accept"
