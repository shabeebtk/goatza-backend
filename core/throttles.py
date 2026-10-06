"""
Throttles for surfaces that anonymous callers can reach.

THROTTLING FAILS OPEN WHEN REDIS IS DOWN, AND THAT IS DELIBERATE.

DRF keeps every rate-limit bucket in the cache. Since core/cache/resilient.py
made the cache degrade instead of raise, a Redis outage means
``SimpleRateThrottle`` reads no history for anyone, so ``allow_request``
answers True and every limit in the app is briefly absent: the login and OTP
limits here and in apps.accounts, the guardian-consent limits, the per-actor
write limits, all of them.

THE TRADE, stated plainly: the alternative is a 500 on every throttled
endpoint, which takes login, signup and password reset down completely. An
unthrottled window is worse for abuse and better for everyone else, and abuse
is bounded by everything else that still applies — authentication, the
guardian and terms gates, ownership checks, and Postgres constraints. None of
those live in the cache.

IT IS STILL SECURITY-RELEVANT, so it must never happen quietly. The backend
logs the first failure of every outage at ERROR (once, not per request), which
is the signal that rate limits are currently off; /healthz reports the cache as
a degraded component at the same time. If either is missing, this trade has
become invisible and is no longer a trade.
"""

from rest_framework.throttling import AnonRateThrottle


class PublicReadThrottle(AnonRateThrottle):
    """
    IP-keyed rate limit for the public read surface (core.public_urls).

    AnonRateThrottle, not messaging.throttles.ActorScopedThrottle: that one
    keys on the logged-in user's pk plus the actor headers, which do not exist
    here. The IP is the only identity an anonymous scrape has.

    A signed-in caller hitting a public endpoint is exempt — AnonRateThrottle
    returns None for an authenticated request, so they fall back to the normal
    'user' bucket instead of sharing a per-IP one with everybody behind the
    same office NAT.
    """

    scope = "public_profile"
