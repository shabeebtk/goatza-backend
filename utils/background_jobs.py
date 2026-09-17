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
"""

import logging
import threading
import time

from django.conf import settings
from django.db import transaction

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

_state_lock = threading.Lock()
_last_publish_failure_at = None  # _clock() reading, or None if never failed


def reset_dispatch_state():
    """Forget any recorded publish failure. For tests."""
    global _last_publish_failure_at
    with _state_lock:
        _last_publish_failure_at = None


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
