"""
The one way a background job is dispatched: ``enqueue(task, ...)``.

WHY THIS EXISTS: the product has to run identically with no worker and no
broker. ``CELERY_ENABLED`` is off by default (core/settings.py), and with it
off nothing here ever opens a socket to Redis — the task simply runs where it
was called, which is what the app did before Celery was installed. With it on,
a broker that is slow, full or gone must not take a request down with it: the
publish is bounded by the transport timeouts in settings, a failure falls back
to running the job right here, and for the next
``CELERY_DISPATCH_FAILURE_COOLDOWN`` seconds this process stops trying the
broker at all, so only the first request in a window pays the timeout.

NOTHING SHOULD CALL ``task.delay()`` OR ``task.apply_async()`` DIRECTLY. Both
bypass the enabled check, the cooldown and the fallback, and ``delay`` in a
disabled environment only "works" because ``CELERY_TASK_ALWAYS_EAGER`` catches
it. The conventions for writing a task are in CLAUDE.md ("Background jobs").

``enqueue`` NEVER RAISES INTO ITS CALLER. A job is a side effect of a request
that has already done its real work; the request must not fail because the
side effect could not be scheduled. Failures are log lines — at ERROR for the
ones that should reach Sentry — and nothing else.

The cooldown is PER PROCESS, in memory, on purpose: Redis may be the very thing
that is down, so it cannot be where the "Redis is down" flag lives.

A BROKER THAT ACCEPTS A PUBLISH SAYS NOTHING ABOUT A WORKER CONSUMING IT. That
is the one failure the cooldown above cannot see: Redis is healthy, the publish
succeeds, and the job sits in a list nobody is draining — emails and pushes
stop arriving and every log line says "queued". ``worker_state`` closes it. The
worker writes a timestamp to the cache once a minute (``core.heartbeat``), and
a dispatch that finds that timestamp OLD runs the job here instead of handing
it to a worker that is not there.
"""

import logging
import threading
import time
from datetime import datetime
from datetime import timezone as datetime_timezone

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

# What _dispatch reports back. "failed" is the inline fallback itself raising;
# a broker publish that fails and then runs inline reports "inline", because
# the job did run.
QUEUED = "queued"
INLINE = "inline"
SKIPPED = "skipped"
FAILED = "failed"

FALLBACK_INLINE = "inline"
FALLBACK_SKIP = "skip"
_FALLBACKS = (FALLBACK_INLINE, FALLBACK_SKIP)

# Indirection so a test can move the clock instead of sleeping through a
# cooldown. Monotonic: a wall-clock adjustment must not reopen or extend it.
_clock = time.monotonic

# The key ``core.heartbeat`` writes and ``worker_state`` reads. Defined HERE,
# next to the rule that interprets it, not next to the task that writes it: the
# meaning of the value is the staleness policy below, and the task is three
# lines that know nothing about it.
#
# WRITTEN WITH NO EXPIRY (timeout=None). A key that expired on its own would
# read as "missing", which is "unknown", which does NOT trigger the fallback —
# so an expiring key would quietly undo the whole check some seconds after the
# worker died. A stale value that lingers IS the signal.
WORKER_LAST_SEEN_KEY = "celery:worker:last_seen"

# What worker_state reports.
WORKER_OK = "ok"
WORKER_STALE = "stale"
WORKER_UNKNOWN = "unknown"

_state_lock = threading.Lock()
_last_publish_failure_at = None  # _clock() reading, or None if never failed
_last_worker_stale_log_at = None  # _clock() reading of the last ERROR we wrote


def reset_dispatch_state():
    """Forget any recorded publish failure. For tests."""
    global _last_publish_failure_at, _last_worker_stale_log_at
    with _state_lock:
        _last_publish_failure_at = None
        _last_worker_stale_log_at = None


def _record_publish_failure():
    global _last_publish_failure_at
    with _state_lock:
        _last_publish_failure_at = _clock()


def _cooldown_remaining():
    """Seconds until the broker is tried again, or 0 if it can be tried now."""
    with _state_lock:
        failed_at = _last_publish_failure_at
    if failed_at is None:
        return 0
    remaining = settings.CELERY_DISPATCH_FAILURE_COOLDOWN - (_clock() - failed_at)
    return remaining if remaining > 0 else 0


def worker_state():
    """
    Whether a worker is alive, as far as this process can tell:
    ``"ok"`` | ``"stale"`` | ``"unknown"``.

    ``core.heartbeat`` writes ``WORKER_LAST_SEEN_KEY`` every 60 seconds
    (CELERY_BEAT_SCHEDULE). This reads it back and compares it with now:

        present and younger than CELERY_WORKER_STALE_AFTER  -> "ok"
        present and older                                   -> "stale"
        missing, unreadable, or not a timestamp              -> "unknown"

    "STALE" IS ONLY EVER A PRESENT-AND-OLD VALUE, AND THAT IS THE WHOLE
    DESIGN. Stale is the answer that makes ``_dispatch`` stop using the broker,
    so it has to mean "a worker was running and has stopped" and nothing else.
    Three ordinary situations produce a MISSING key and none of them is a dead
    worker:

      * A FRESH DEPLOY. Beat has not ticked yet, so for up to a minute after
        every single deploy there is no key at all.
      * A FLUSHED REDIS. ``cache.clear()`` in a test run, an eviction, a
        restarted Redis with no persistence — the key is gone while the worker
        it describes is perfectly healthy.
      * A DEGRADED CACHE. The resilient backend answers a read with the
        caller's default while its breaker is open (core/cache/resilient.py),
        so a web process that cannot reach Redis reads "missing" no matter what
        is in there.

    Treating any of those as "stale" would inline every job in every request
    across the whole fleet, at once, for as long as the condition lasted —
    turning a missing cache key into the outage the fallback exists to prevent.
    Unknown means unknown: keep publishing, and let the publish itself fail if
    the broker is really gone. That path is already covered.

    Never raises: a cache that misbehaves is an unknown, not an error.
    """
    try:
        last_seen = cache.get(WORKER_LAST_SEEN_KEY)
    except Exception:
        # The resilient backend swallows RedisError itself, so reaching here is
        # something else. WARNING, not ERROR: the answer below is safe, and
        # this runs on every dispatch — an ERROR would be a Sentry event per
        # job for the duration.
        logger.warning(
            "background_jobs | worker heartbeat unreadable", exc_info=True
        )
        return WORKER_UNKNOWN

    if not last_seen:
        return WORKER_UNKNOWN

    try:
        seen_at = datetime.fromisoformat(last_seen)
    except (TypeError, ValueError):
        # Somebody else's value under our key, or a format change mid-deploy.
        logger.warning(
            "background_jobs | worker heartbeat is not a timestamp | value=%r",
            last_seen,
        )
        return WORKER_UNKNOWN

    # The task writes an aware UTC timestamp; a naive one could only come from
    # an older deploy's value, and UTC is what that one meant too.
    if seen_at.tzinfo is None:
        seen_at = seen_at.replace(tzinfo=datetime_timezone.utc)

    # WALL CLOCK, not monotonic, and it has to be: the writer is a different
    # process on a different container from the reader, and monotonic readings
    # cannot be compared across them. Both are NTP-synced and the threshold is
    # minutes, so ordinary skew is noise. Skew the other way (worker ahead of
    # web) gives a negative age, which reads as "ok" — the safe direction.
    age_seconds = (timezone.now() - seen_at).total_seconds()

    if age_seconds > settings.CELERY_WORKER_STALE_AFTER:
        return WORKER_STALE

    return WORKER_OK


def _should_log_worker_stale():
    """
    True at most once per ``CELERY_DISPATCH_FAILURE_COOLDOWN`` per process.

    A dead worker is a condition every single dispatch rediscovers, and the
    line that reports it is at ERROR so Sentry raises an event. Unthrottled
    that is one event per job for as long as the worker is down, which buries
    the one that mattered and spends the Sentry quota on a single fact. Same
    shape as the publish-failure cooldown, deliberately: one event a minute.

    A SECOND TIMESTAMP, not ``_last_publish_failure_at``. That one decides
    whether the broker gets dialled at all, and writing to it here would stop
    this process publishing for a minute every time it logged — conflating "I
    already said this" with "do not touch Redis".
    """
    global _last_worker_stale_log_at
    with _state_lock:
        now = _clock()
        last = _last_worker_stale_log_at
        if (
            last is not None
            and (now - last) < settings.CELERY_DISPATCH_FAILURE_COOLDOWN
        ):
            return False
        _last_worker_stale_log_at = now
        return True


def _task_name(task):
    return getattr(task, "name", None) or repr(task)


def _run_fallback(task, args, kwargs, fallback, options, reason):
    name = _task_name(task)

    # A delay only means something to a worker. Whatever asked for it gets the
    # job now (or not at all), and should know that from the log.
    if "countdown" in options or "eta" in options:
        logger.info(
            "background_jobs | delay dropped, fallback has no scheduler | "
            "task=%s | countdown=%s | eta=%s",
            name, options.get("countdown"), options.get("eta"),
        )

    if fallback == FALLBACK_SKIP:
        logger.info(
            "background_jobs | skipped | task=%s | reason=%s", name, reason
        )
        return SKIPPED

    try:
        # __call__ runs the task function in this thread, broker or not. Not
        # task.apply(): that swallows the exception into an EagerResult and
        # the log line below is the only trace a failed job leaves.
        task(*args, **kwargs)
    except Exception:
        logger.error(
            "background_jobs | inline run raised | task=%s | reason=%s",
            name, reason, exc_info=True,
        )
        return FAILED

    logger.info(
        "background_jobs | ran inline | task=%s | reason=%s", name, reason
    )
    return INLINE


def _dispatch(task, args=(), kwargs=None, *, fallback=FALLBACK_INLINE, **options):
    """
    The dispatch itself, with no on_commit deferral. Returns one of QUEUED /
    INLINE / SKIPPED / FAILED so tests can assert which path ran; ``enqueue``
    is the public entry point and returns nothing.

    ``settings.CELERY_ENABLED`` is read HERE, on every call, never at import:
    tests flip it with override_settings.
    """
    args = tuple(args)
    kwargs = dict(kwargs or {})
    name = _task_name(task)

    if not settings.CELERY_ENABLED:
        return _run_fallback(
            task, args, kwargs, fallback, options, reason="celery disabled"
        )

    remaining = _cooldown_remaining()
    if remaining:
        logger.info(
            "background_jobs | broker in cooldown, not tried | task=%s | "
            "retry_in=%.0fs",
            name, remaining,
        )
        return _run_fallback(
            task, args, kwargs, fallback, options, reason="broker cooldown"
        )

    # BROKER UP, WORKER DEAD. Checked here, after the cooldown and before the
    # publish, because this is the one failure a successful apply_async cannot
    # reveal: the job would be accepted, queued, and never run. Only a
    # PRESENT-AND-OLD heartbeat gets here — see worker_state on why unknown
    # must not.
    if worker_state() == WORKER_STALE:
        if _should_log_worker_stale():
            # ERROR so the Sentry logging integration raises an event: nothing
            # is draining the queue, which is an outage even though every
            # request still succeeds. Throttled to one per cooldown window.
            logger.error(
                "background_jobs | WORKER STALE (no heartbeat for >%ss), "
                "running jobs inline | task=%s | fallback=%s",
                settings.CELERY_WORKER_STALE_AFTER, name, fallback,
            )
        else:
            logger.debug(
                "background_jobs | worker still stale | task=%s", name
            )
        return _run_fallback(
            task, args, kwargs, fallback, options, reason="worker stale"
        )

    try:
        task.apply_async(args=args, kwargs=kwargs, **options)
    except Exception:
        _record_publish_failure()
        # ERROR so the Sentry logging integration raises an event: a broker
        # that cannot be published to is an outage, even though the request
        # that hit it is about to succeed anyway.
        logger.error(
            "background_jobs | publish FAILED, broker paused for %ss | "
            "task=%s | fallback=%s",
            settings.CELERY_DISPATCH_FAILURE_COOLDOWN, name, fallback,
            exc_info=True,
        )
        return _run_fallback(
            task, args, kwargs, fallback, options, reason="publish failed"
        )

    logger.info("background_jobs | queued | task=%s", name)
    return QUEUED


def enqueue(
    task, args=(), kwargs=None, *, fallback=FALLBACK_INLINE, on_commit=True, **options
):
    """
    Dispatch ``task`` — to the worker if Celery is enabled and the broker is
    healthy, otherwise by running it here (``fallback="inline"``) or not at all
    (``fallback="skip"``). ``options`` go to ``apply_async`` (``countdown``,
    ``eta``, ``queue`` ...).

    ``on_commit=True`` (the default) defers everything through
    ``transaction.on_commit``, the same pattern the services already use for
    emails and pushes: a job must never see a row its transaction has not
    committed yet, and a rolled-back request must not leave a job behind.
    Outside an atomic block Django runs the callback immediately.

    Returns None — with ``on_commit=True`` nothing has happened yet when this
    returns, so there is no honest result to give. Never raises, except for a
    ``fallback`` value that is not one of the two known ones, which is a
    programming error and not a runtime condition.
    """
    if fallback not in _FALLBACKS:
        raise ValueError(f"fallback must be one of {_FALLBACKS}, got {fallback!r}")

    args = tuple(args)
    kwargs = dict(kwargs or {})

    def _run():
        try:
            _dispatch(task, args, kwargs, fallback=fallback, **options)
        except Exception:
            # _dispatch guards every path it knows about; this catches the one
            # it does not, because an on_commit callback that raises would
            # surface after the response was already decided.
            logger.error(
                "background_jobs | dispatch raised unexpectedly | task=%s",
                _task_name(task), exc_info=True,
            )

    if on_commit:
        transaction.on_commit(_run)
    else:
        _run()
