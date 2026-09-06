"""
The infrastructure liveness probe. NOT part of the public data surface.

A PLAIN DJANGO VIEW on purpose — deliberately not a DRF ``APIView``, and the
one endpoint in this app that is neither. Everything DRF wraps a view in is
wrong for a probe Render polls every few seconds, forever:

  * JWTAuthentication — the probe has no token and never will.
  * ``HasAcceptedCurrentTerms`` (the default permission pair, see
    REST_FRAMEWORK in core.settings) — a health check has not accepted terms.
  * AnonRateThrottle at 20/min — a probe on a 10s interval is 6/min from ONE
    address, and every other anonymous caller behind the same proxy hop would
    be sharing that bucket with it. A throttled health check reads as a dead
    service and gets the instance recycled.

It also stays out of ``core.public_urls`` (routed from ``core.urls`` instead):
that file is an allow-list of anonymous reads of OUR data, and this returns two
booleans about our own plumbing. See the note at the top of it.

RENDER: the Health Check Path must be set to ``/healthz`` in the service's
dashboard (Settings → Health Check Path). There is no way to declare that from
code, and until it is set Render falls back to "the port is open", which a
process with a dead database still satisfies.

WHAT IT DOES NOT DO: no exception text, no hostnames, no connection strings in
the body — this URL is open to the internet. Failures are named by COMPONENT
and the detail goes to the log (and therefore to Sentry, at ERROR).
"""

import logging

from django.core.cache import cache
from django.db import connection
from django.http import JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

logger = logging.getLogger(__name__)

# Written and read back inside one request. Short TTL because nothing ever
# reads it again — if the probe dies between the set and the get, the key
# expiring on its own is the only cleanup there is.
_PROBE_KEY = "healthz:probe"
_PROBE_TTL = 10

OK = "ok"
ERROR = "error"


def _check_database():
    """True if a connection can be opened and a trivial query answered."""
    try:
        # ensure_connection alone can pass on a connection that was reused from
        # the CONN_MAX_AGE=60 pool and has since been killed server-side, so
        # actually send a statement.
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        return True
    except Exception:
        logger.error("healthz | database check failed", exc_info=True)
        return False


def _check_cache():
    """
    True if the cache round-trips a value.

    A round trip, not a bare ``set``: the Redis client buffers and a set that
    never reaches the server can still return without raising, so the read back
    is the part that proves anything. In production this cache is REDIS_URL; on
    a checkout with no REDIS_URL it is LocMemCache, which passes trivially —
    correct, because there is nothing to be down.
    """
    try:
        probe = str(id(object()))
        cache.set(_PROBE_KEY, probe, _PROBE_TTL)
        return cache.get(_PROBE_KEY) == probe
    except Exception:
        logger.error("healthz | cache (redis) check failed", exc_info=True)
        return False


@never_cache
@require_GET
def healthz(request):
    """
    GET /healthz → 200 {"status": "ok", "db": "ok", "redis": "ok"}
                   503 {"status": "degraded", "db": "ok", "redis": "error"}

    Each check is independently guarded so a dead Redis still reports the true
    state of the database — a probe that stops at the first failure tells you
    one thing is broken and hides whether the other is too.
    """
    db_ok = _check_database()
    cache_ok = _check_cache()
    healthy = db_ok and cache_ok

    if not healthy:
        # ERROR level, so the Sentry logging integration raises an event —
        # this is the line that pages somebody.
        logger.error(
            "healthz | degraded | db=%s | redis=%s",
            OK if db_ok else ERROR,
            OK if cache_ok else ERROR,
        )

    return JsonResponse(
        {
            "status": "ok" if healthy else "degraded",
            "db": OK if db_ok else ERROR,
            "redis": OK if cache_ok else ERROR,
        },
        status=200 if healthy else 503,
    )
