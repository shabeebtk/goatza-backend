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

ONLY THE DATABASE DECIDES THE HTTP STATUS. Render recycles a container that
answers non-200 here, and since the cache became resilient
(core/cache/resilient.py) a dead Redis no longer stops the app serving — it
makes it slower. Recycling the web service over it would turn a degradation
into an outage, and the replacement container would come up just as unable to
reach Redis. So a degraded cache is reported as a named component at 200, and
only an unreachable Postgres is a 503.
"""

import logging

from django.core.cache import cache
from django.db import connection
from django.http import JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from utils.cache import cache_is_degraded

logger = logging.getLogger(__name__)

# Written and read back inside one request. Short TTL because nothing ever
# reads it again — if the probe dies between the set and the get, the key
# expiring on its own is the only cleanup there is.
_PROBE_KEY = "healthz:probe"
_PROBE_TTL = 10

OK = "ok"
ERROR = "error"
# Reachable but not in use: the breaker is open, so reads miss and writes drop.
# A warning, never a reason to recycle the container.
DEGRADED = "degraded"


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
    True if the cache is actually serving.

    TWO QUESTIONS, BOTH NECESSARY.

    ``cache_is_degraded()`` is the backend's own breaker state. It is the
    authoritative answer now that the backend swallows RedisError: the probe
    below would otherwise sail through a total Redis outage, reporting healthy
    at exactly the moment the truth matters.

    The round trip is still here because the breaker cannot see everything. A
    set that the client buffered and never delivered raises nothing and opens
    no window, and the read back is the only thing that catches it. So: not
    degraded AND the value comes back.

    In production this cache is REDIS_URL; on a checkout with no REDIS_URL it
    is LocMemCache, which has no breaker and round-trips fine — correct,
    because there is nothing to be down.
    """
    if cache_is_degraded():
        # Already logged at ERROR by the backend, once for the whole outage.
        # Logging again per probe would be a line every few seconds forever.
        return False

    try:
        probe = str(id(object()))
        cache.set(_PROBE_KEY, probe, _PROBE_TTL)
        return cache.get(_PROBE_KEY) == probe
    except Exception:
        # The resilient backend does not raise RedisError, so reaching here
        # means something else did — a serialization bug, a misconfiguration.
        # Worth its own line.
        logger.error("healthz | cache (redis) check failed", exc_info=True)
        return False


@never_cache
@require_GET
def healthz(request):
    """
    GET /healthz → 200 {"status": "ok",       "db": "ok",    "redis": "ok"}
                   200 {"status": "degraded", "db": "ok",    "redis": "degraded"}
                   503 {"status": "degraded", "db": "error", "redis": "ok"}

    THE DATABASE ALONE DECIDES THE STATUS CODE. Postgres is the system of
    record: without it this process can serve nothing and deserves to be
    replaced. Redis is an optimisation the app now runs without, so a dead one
    is reported and not acted on — recycling the container would replace a
    slow service with an equally slow service, having dropped every in-flight
    request to do it.

    Both checks still run and both are still reported. A probe that stops at
    the first failure tells you one thing is broken and hides whether the
    other is too.
    """
    db_ok = _check_database()
    cache_ok = _check_cache()

    # The status WORD covers both components; the status CODE covers only the
    # one Render should act on.
    healthy = db_ok and cache_ok

    if not db_ok:
        # ERROR level, so the Sentry logging integration raises an event —
        # this is the line that pages somebody.
        logger.error(
            "healthz | database unreachable | db=%s | redis=%s",
            ERROR, OK if cache_ok else DEGRADED,
        )
    elif not cache_ok:
        # WARNING, not ERROR: the backend already logged the outage itself at
        # ERROR, once, and /healthz is polled every few seconds — an ERROR
        # here would be a Sentry event per poll for the whole outage.
        logger.warning("healthz | cache degraded, still serving | db=ok")

    return JsonResponse(
        {
            "status": OK if healthy else DEGRADED,
            "db": OK if db_ok else ERROR,
            "redis": OK if cache_ok else DEGRADED,
        },
        status=200 if db_ok else 503,
    )
