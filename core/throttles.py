"""Throttles for surfaces that anonymous callers can reach."""

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
