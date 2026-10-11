# recruitments/tasks.py
"""
Celery tasks for recruitments.

THIN, per CLAUDE.md ("Background jobs"): a task loads what it needs by id and
calls an existing service or command, so the same code runs from a view, a
management command or a worker.

BEAT DRIVES BOTH OF THESE (CELERY_BEAT_SCHEDULE in core/settings.py):

    recruitments.dispatch_announcements   every 5 minutes
    recruitments.send_trial_reminders     hourly, at minute 0 IST

They used to be Render Cron lines running the commands directly. THE COMMANDS
REMAIN, unchanged and still the way to run either job by hand — with
``--dry-run``, with a ``--limit``, or to catch up after an outage:

    python manage.py dispatch_announcements
    python manage.py send_trial_reminders

That is the whole point of the wrapper being this thin: one implementation, two
triggers, so a hand-run and a scheduled run can never drift. Retire the Render
Cron services only once the worker's heartbeat reads "ok" at /healthz — until
then they are the thing actually doing the work.

``send_trial_reminders`` MUST STAY AT MINUTE 0: the command is gated on the
hour, so an entry at :30 would run on time and find the window shut, every
hour, reporting nothing wrong.
"""

import logging

from celery import shared_task
from django.core.management import call_command

logger = logging.getLogger(__name__)


@shared_task(name="recruitments.dispatch_announcements")
def dispatch_announcements(limit=None):
    """
    Send the announcement deliveries that are still pending.

    Explicit ``name=`` rather than the path-derived default: the move into
    ``apps/`` already changed every import path once, and a task sitting in a
    queue under a path-derived name would have been undeliverable after that
    deploy.

    SAFE TO RUN TWICE, which ``task_acks_late`` makes a routine occurrence
    rather than an edge case. The command claims only PENDING rows with
    ``select_for_update(skip_locked=True)`` and SENT is terminal, so a
    redelivered task either finds nothing to do or picks up the rows the first
    run had not locked. Nobody is emailed twice.

    Takes a plain int, never a model instance — the serializer is JSON.
    """
    kwargs = {}
    if limit is not None:
        kwargs["limit"] = int(limit)

    call_command("dispatch_announcements", **kwargs)


@shared_task(name="recruitments.send_trial_reminders")
def send_trial_reminders():
    """
    Remind confirmed applicants whose trial is tomorrow.

    Explicit ``name=`` for the same reason as the task above: Celery's default
    is the dotted import path, and the move into ``apps/`` already changed
    every import path once.

    SAFE TO RUN TWICE, which is routine under ``task_acks_late``. The command
    is gated on the hour and stamps ``trial_reminder_sent_at`` as it goes, so
    a redelivered task either finds the window closed or finds every due row
    already stamped. Nobody is reminded twice.

    Takes no arguments — what is due is a database question, deliberately not
    a scheduling one. See the command's docstring on why reminders are not
    scheduled ahead with ``eta``.
    """
    call_command("send_trial_reminders")
