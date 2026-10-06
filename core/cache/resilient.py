"""
The cache backend the app runs on: Redis, but never load-bearing.

WHY THIS EXISTS. ``django.core.cache.backends.redis.RedisCache`` is Django's
own backend and has no ``IGNORE_EXCEPTIONS`` option — that is a django-redis
feature, and we are not on django-redis. So an unreachable Redis raised
``redis.exceptions.ConnectionError`` straight into whatever view touched the
cache, and because ``TokenRefreshAPIView`` writes a 60-second replay entry on
every single call, ``POST /user/token/refresh`` answered 500 and the whole app
was unusable without Redis.

Postgres is the system of record. Redis is an optimisation with a memory.
Losing it must make the app SLOWER, never DEAD.

WHAT EACH OPERATION DEGRADES TO. Every wrapped call answers the value a caller
already has to handle for a cache MISS, so no caller needs to learn a new
shape:

    get / get_many      the caller's default / {}   (a miss)
    set / set_many      the write is dropped
    add                 False                        (see below)
    delete / touch      False
    has_key             False
    incr / decr         None                         (see below)
    clear               nothing happens

ONLY ``redis.exceptions.RedisError`` IS CAUGHT — ConnectionError and
TimeoutError are subclasses of it, which is the whole set we mean. Broad
``Exception`` is deliberately NOT caught: a pickling failure on a value we put
in ourselves is a bug in our code, and a bug that silently turns into a cache
miss is a bug nobody ever finds.

``add`` IS A LATCH, AND FALSE IS THE SAFE ANSWER. Two callers use it as
"count this once" — the CV view counter and the applicant-alert claim. False
means "somebody else already has the latch", so the degraded answer is that
the thing is NOT counted. Under-counting is the correct failure here:
double-counting corrupts a number the org reads, and a 500 loses the whole
request. A view count that is low for the minutes Redis was down is the
cheapest of the three.

``incr`` GUARDS MONEY, SO IT RETURNS None, NOT 0.
``apps.places.services.places_service`` counts Google Places calls against a
paid daily cap. Returning 0 would read as "nothing spent today" and hand out
an unmetered budget for as long as Redis stayed down — failing OPEN on spend.
None means "unknown", and ``check_budget`` treats unknown as a reason to
REFUSE the paid call. See ``utils.cache.cache_is_degraded``.

Note ``incr`` on a live Redis still raises ``ValueError`` for a missing key —
that is Django's documented contract, it is not a Redis failure, and
places_service depends on it (it calls ``add`` first precisely to avoid it).

THE CIRCUIT BREAKER IS THE POINT. Catching the error alone would leave every
request paying the TCP connect timeout, which turns a fast app into an
unusably slow one instead of a broken one — a worse outcome, not a better one.
After one failure this process stops dialling Redis for
``settings.CACHE_FAILURE_COOLDOWN`` seconds and goes straight to the degraded
answer, so only the first request in a window pays anything at all. The 1s
``socket_connect_timeout`` in OPTIONS (core.settings) is what bounds that
first one.

The cooldown is PER PROCESS, IN MEMORY, and on a MONOTONIC clock — the same
three choices ``utils.background_jobs`` makes for the broker, for the same
reasons. Redis may be the very thing that is down, so the "Redis is down" flag
cannot live in Redis; and a wall-clock adjustment must not silently reopen or
extend the window.

LOGGING IS ONCE PER WINDOW, NOT ONCE PER REQUEST. A failure that actually
dialled logs at ERROR, so the Sentry logging integration raises an event and
somebody finds out — and the cooldown is what bounds it, because nothing dials
inside an open window. Skips inside the window log at DEBUG; recovery logs at
INFO. A dead Redis under load would otherwise write a line per request and bury
the one line that mattered.
"""

import logging
import threading
import time

from django.conf import settings
from django.core.cache.backends.base import DEFAULT_TIMEOUT
from django.core.cache.backends.redis import RedisCache
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)

# Indirection so a test can move the clock instead of sleeping through a
# cooldown, exactly as utils.background_jobs does.
_clock = time.monotonic

_state_lock = threading.Lock()

# _clock() reading of the most recent failure, or None if Redis has not failed
# in this process. Module level, not instance level: `caches` may hand out more
# than one backend object over a process's life, and they are all talking to
# the same Redis.
_last_failure_at = None


def reset_cache_state():
    """Forget any recorded failure. For tests."""
    global _last_failure_at
    with _state_lock:
        _last_failure_at = None


def last_failure_at():
    """
    The ``time.monotonic()`` reading of the last failure, or None.

    A monotonic reading, NOT a wall clock: it is only meaningful compared with
    other readings from this process, which is all the cooldown needs.
    """
    with _state_lock:
        return _last_failure_at


def _cooldown_remaining():
    """Seconds until Redis is dialled again, or 0 if it can be tried now."""
    # LOCK-FREE FAST PATH. This runs before every cache operation in the app,
    # and the healthy answer is always "no window open". Reading a module
    # global is atomic under the GIL, and the only way to be wrong is to miss
    # a failure recorded microseconds ago — which costs one extra dial to a
    # Redis that is about to fail again anyway.
    if _last_failure_at is None:
        return 0

    with _state_lock:
        failed_at = _last_failure_at
    if failed_at is None:
        return 0
    remaining = settings.CACHE_FAILURE_COOLDOWN - (_clock() - failed_at)
    return remaining if remaining > 0 else 0


def is_degraded():
    """
    True while this process is knowingly skipping Redis.

    THE HONEST READ for anything that needs to know whether a write landed:
    /healthz reports it as a component, and places_service treats it as
    "usage unknown". It is not a ping — it says nothing about Redis right now,
    only that we failed recently enough to still be backing off.
    """
    return _cooldown_remaining() > 0


def _record_failure(operation, exc):
    """
    Open the window and log it at ERROR.

    ONE LINE PER WINDOW, and the window itself is what rate-limits this rather
    than a second flag: ``_guarded`` only dials Redis when the cooldown has
    expired, so this is reached at most once per CACHE_FAILURE_COOLDOWN no
    matter how much traffic there is. A long outage therefore leaves a steady
    ~2 lines a minute — enough to see it is still happening, nowhere near the
    line-per-request that would bury it.

    Two threads can race past the cooldown check and both log; that is bounded
    by the worker count and is not worth a lock held across a log call.
    """
    global _last_failure_at

    with _state_lock:
        _last_failure_at = _clock()

    # ERROR so the Sentry logging integration raises an event. The request that
    # hit this is about to succeed anyway, which is exactly why it has to be
    # loud: nothing else will report the outage.
    #
    # THROTTLING READS THROUGH THIS CACHE, so this line is also the only signal
    # that rate limits are failing open right now. See core.throttles.
    logger.error(
        "cache | Redis %s FAILED, skipping Redis for %ss | %s: %s",
        operation, settings.CACHE_FAILURE_COOLDOWN,
        type(exc).__name__, exc,
    )


def _record_success():
    """Close the window. Logs INFO only on an actual recovery."""
    global _last_failure_at

    # Same fast path as _cooldown_remaining, and the same reason: on a healthy
    # Redis there is never anything to clear, and this is called on every hit.
    if _last_failure_at is None:
        return

    with _state_lock:
        recovered = _last_failure_at is not None
        _last_failure_at = None

    if recovered:
        logger.info("cache | Redis recovered, serving from cache again")


class ResilientRedisCache(RedisCache):
    """
    ``RedisCache`` that answers like a cache miss instead of raising.

    Every override is the same three lines through ``_guarded``: skip if the
    breaker is open, call up, and turn a ``RedisError`` into the degraded
    value. Nothing here changes what a HEALTHY Redis does.
    """

    def _guarded(self, operation, degraded, call, *args, **kwargs):
        if _cooldown_remaining():
            logger.debug("cache | %s skipped, Redis in cooldown", operation)
            return degraded

        try:
            result = call(*args, **kwargs)
        except RedisError as exc:
            _record_failure(operation, exc)
            return degraded

        # Only after a call that actually reached Redis, so a window is closed
        # by evidence and not by the absence of traffic.
        _record_success()
        return result

    # ── Reads ────────────────────────────────────────────────────

    def get(self, key, default=None, version=None):
        # The caller's own default, so a degraded read is indistinguishable
        # from a miss — which every get() caller already handles.
        return self._guarded(
            "get", default, super().get, key, default, version
        )

    def get_many(self, keys, version=None):
        return self._guarded("get_many", {}, super().get_many, keys, version)

    def has_key(self, key, version=None):
        return self._guarded("has_key", False, super().has_key, key, version)

    # ── Writes ───────────────────────────────────────────────────

    def set(self, key, value, timeout=DEFAULT_TIMEOUT, version=None):
        return self._guarded(
            "set", None, super().set, key, value, timeout, version
        )

    def set_many(self, data, timeout=DEFAULT_TIMEOUT, version=None):
        # set_many returns the keys it FAILED to set. Degraded, that is all of
        # them — saying [] would claim a write that never happened.
        return self._guarded(
            "set_many", list(data), super().set_many, data, timeout, version
        )

    def add(self, key, value, timeout=DEFAULT_TIMEOUT, version=None):
        # False = "somebody else holds the latch", so a caller counting once
        # does not count. Under-counting on purpose — see the module docstring.
        return self._guarded(
            "add", False, super().add, key, value, timeout, version
        )

    def touch(self, key, timeout=DEFAULT_TIMEOUT, version=None):
        # False, not None: touch's contract is "did the key exist", and nothing
        # was extended here. Falsy either way, so no caller can tell.
        return self._guarded(
            "touch", False, super().touch, key, timeout, version
        )

    def delete(self, key, version=None):
        return self._guarded("delete", False, super().delete, key, version)

    def delete_many(self, keys, version=None):
        return self._guarded(
            "delete_many", None, super().delete_many, keys, version
        )

    def clear(self):
        return self._guarded("clear", None, super().clear)

    # ── Counters ─────────────────────────────────────────────────

    def incr(self, key, delta=1, version=None):
        # None = "unknown", never 0. A budget counter that reads 0 while Redis
        # is down is an unmetered budget. See the module docstring.
        return self._guarded("incr", None, super().incr, key, delta, version)

    # decr is NOT overridden: BaseCache.decr is implemented as
    # self.incr(key, -delta), so it already routes through the guard above and
    # a second wrapper would only double-count the failure.

    # ── State, for callers that must know a write did not land ───

    def is_degraded(self):
        """Instance access to the module state, so ``cache.is_degraded()``
        works through Django's lazy proxy. See ``utils.cache``."""
        return is_degraded()

    def last_failure_at(self):
        return last_failure_at()
