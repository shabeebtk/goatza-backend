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
