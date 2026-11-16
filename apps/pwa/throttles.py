"""Throttle for the install/open telemetry endpoint."""

from rest_framework.throttling import AnonRateThrottle


class AppEventsThrottle(AnonRateThrottle):
    """
    60/hour per IP on POST /app/events (``app_events`` in
    DEFAULT_THROTTLE_RATES).

    PER IP FOR EVERY CALLER, signed in or not. A plain AnonRateThrottle
    returns no key for an authenticated request, and this view lists no other
    throttle, so a signed-in caller would have no limit at all on an
    anonymous-friendly write. Keying on the IP for everyone closes that.

    An honest client sends a handful of events a day, so 60 an hour leaves
    plenty of room for several devices behind one NAT. When it does trip, an
    open simply goes uncounted: under-counting is the right way for a number
    like this to fail.

    The IP is part of the cache key for the length of the window, as with
    every IP-keyed throttle in the app. It is never written to the table.
    """

    scope = "app_events"

    def get_cache_key(self, request, view):
        return self.cache_format % {
            "scope": self.scope,
            "ident": self.get_ident(request),
        }
