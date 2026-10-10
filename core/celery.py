"""
The Celery application. Imported by ``core/__init__.py`` so that it exists
whenever Django does — ``shared_task`` decorators and ``autodiscover_tasks``
bind to whichever app is current, and an app that only appears when the worker
starts would leave the web process's tasks unregistered.

Configuration lives in the CELERY block of ``core/settings.py`` and nowhere
else (``config_from_object`` with the ``CELERY_`` namespace). Nothing should
dispatch a task directly: ``utils.background_jobs.enqueue`` is the one entry
point, and it is what makes the product run identically with no worker and no
broker.

TASK NAMES ARE ALWAYS SET EXPLICITLY (``name="<app>.<verb>"``). Celery's
default is the dotted import path, and the move into ``apps/`` already changed
every import path once — a task sitting in a queue under the old name would
have been undeliverable after that deploy. See "Background jobs" in CLAUDE.md.
"""

import logging
import os

from celery import Celery

logger = logging.getLogger(__name__)

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")

app = Celery("goatza")
app.config_from_object("django.conf:settings", namespace="CELERY")

# Picks up apps/<app>/tasks.py for every entry in INSTALLED_APPS. Lazy: the
# modules are imported on first use, not here, so settings are fully loaded.
app.autodiscover_tasks()


@app.task(name="core.ping")
def ping():
    """
    End-to-end pipeline check: publish → broker → worker → log line. Dispatch
    it through ``utils.background_jobs.enqueue`` and watch the worker's output.
    """
    logger.info("celery | ping | pong")
    return "pong"


@app.task(name="core.heartbeat", acks_late=False, ignore_result=True)
def heartbeat():
    """
    Write "a worker was alive at this moment" to the cache, once a minute.

    THE ONE FAILURE NOTHING ELSE SEES. ``utils.background_jobs`` bounds a sick
    broker and falls back when a publish fails, but a broker that ACCEPTS the
    publish proves nothing about anybody consuming it: with the worker dead and
    Redis healthy, every request succeeds, every log line says "queued", and no
    email or push ever arrives. This timestamp is what makes that visible \u2014
    ``worker_state()`` reads it, ``_dispatch`` runs jobs inline when it is old,
    and /healthz reports it as a component.

    ``timeout=None`` \u2014 NO EXPIRY, on purpose. The reader treats a missing key
    as "unknown" and keeps publishing (a fresh deploy and a flushed Redis both
    look like missing), so a key that expired on its own would read as healthy
    minutes after the worker died. A stale value that lingers IS the signal.

    EVERY FAILURE HERE IS SWALLOWED. A degraded cache must make the heartbeat
    unknown, never an error: the write is already dropped silently by the
    resilient backend (core/cache/resilient.py), and the try/except covers
    anything else. A worker that retried or crashed over its own liveness probe
    would be the probe taking down the thing it measures.

    ``acks_late=False`` for the reason the other tasks have it (CLAUDE.md,
    "Background jobs"), plus one of its own: a redelivered heartbeat would
    write a timestamp from before the crash, which is worse than no write.
    """
    # Imported inside the function: this module is imported from
    # core/__init__.py, before Django has finished setting up.
    from django.core.cache import cache
    from django.utils import timezone

    from utils.background_jobs import WORKER_LAST_SEEN_KEY

    now = timezone.now()

    try:
        # An ISO-8601 string rather than a float: the value of an ops signal is
        # being readable with `redis-cli GET celery:worker:last_seen` at 3am.
        cache.set(WORKER_LAST_SEEN_KEY, now.isoformat(), timeout=None)
    except Exception:
        logger.warning(
            "celery | heartbeat | could not write last_seen", exc_info=True
        )
        return

    logger.info("celery | heartbeat | last_seen=%s", now.isoformat())
