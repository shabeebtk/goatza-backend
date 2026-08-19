"""Throttle for the one anonymous WRITE on the public surface."""

from rest_framework.throttling import AnonRateThrottle


class WaitlistSignupThrottle(AnonRateThrottle):
    """
    IP-keyed limit on joining the waitlist. 5/hour (``waitlist_signup`` in
    DEFAULT_THROTTLE_RATES).

    MUST BE SET EXPLICITLY ON THE VIEW. ``core.views.base_views.PublicAPIView``
    already assigns ``throttle_classes = [PublicReadThrottle]``, which is a
    60/min read budget — appropriate for a shared profile link opening in a
    group chat, absurd for a form that writes a row. Subclassing PublicAPIView
    inherits that attribute wholesale, so the create view overrides
    ``throttle_classes`` rather than adding to it. Forgetting the override does
    not fail loudly; it just leaves the signup endpoint on the read limit.

    Tight on purpose. Everything behind this endpoint is unauthenticated and
    permanent: a row, a sequential number somebody will screenshot, and an
    email to me. Five an hour is more than a household sharing one connection
    needs and far less than a script wants.

    Like every AnonRateThrottle, this returns None for an authenticated caller,
    who falls through to the standard 'user' bucket. Pre-launch there is nobody
    to log in, and after launch a signed-in player has no reason to be here.
    """

    scope = "waitlist_signup"
