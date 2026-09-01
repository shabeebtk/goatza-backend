"""Throttles for the two "report a problem" writes."""

from rest_framework.throttling import AnonRateThrottle, UserRateThrottle


class ProblemReportThrottle(UserRateThrottle):
    """
    5/hour on filing a problem report while signed in (``support_report`` in
    DEFAULT_THROTTLE_RATES).

    Per USER, not per actor — the same choice moderation.throttles.ReportThrottle
    makes and for the same reason: an actor-scoped bucket hands one person a
    fresh five for every org they belong to, which turns a per-person budget
    into a per-membership one. The budget belongs to the human behind the
    headers, and a bug is experienced by a human anyway.

    Five is generous for what this is. Somebody hitting a genuinely broken
    screen files one report, maybe two if they find a second way to break it;
    past that they are typing into the box instead of telling us anything new.
    Nothing is lost when it trips — the reports already filed stand.
    """

    scope = "support_report"


class PublicProblemReportThrottle(AnonRateThrottle):
    """
    3/hour on filing a problem report while logged out
    (``support_report_public`` in DEFAULT_THROTTLE_RATES).

    MUST BE SET EXPLICITLY ON THE VIEW. ``core.views.base_views.PublicAPIView``
    assigns ``throttle_classes = [PublicReadThrottle]``, a 60/min READ budget,
    and a subclass inherits that attribute wholesale — forgetting the override
    does not fail loudly, it just leaves an anonymous write endpoint on a read
    limit. Same override and same reasoning as
    waitlist.throttles.WaitlistSignupThrottle.

    Per IP and tighter than the authenticated bucket. Nobody legitimately files
    three bugs an hour while logged out: the logged-out report exists for the
    handful of screens somebody can reach without a session — login, signup,
    OTP — and there are not three of those to break at once. Everything behind
    it is unauthenticated and permanent.

    Like every AnonRateThrottle this returns None for an authenticated caller,
    who falls through to their own bucket instead. That matters here: a
    signed-in user whose client hits the public route must not share a counter
    with everyone else behind the same NAT.
    """

    scope = "support_report_public"
