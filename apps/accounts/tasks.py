# accounts/tasks.py
"""
Celery tasks for accounts — the two nightly sweeps, and the registration point
for ``emails.send``.

The sweeps are THIN, per CLAUDE.md ("Background jobs"): each one is a
``call_command`` of the management command that already exists, so the same
code runs whether beat triggers it or somebody types it into a shell. The
commands keep their own ``--dry-run`` flags and remain the way to run them by
hand.

WHY THE EMAIL TASK IS RE-EXPORTED HERE: ``app.autodiscover_tasks()``
(core/celery.py) imports ``<app>/tasks.py`` for every entry in INSTALLED_APPS
and nothing else. ``utils/emails.py`` is not inside an app, so its
``@shared_task`` decorator never runs in the worker unless some other import
happens to pull the module in — and the worker imports models and task modules,
not the views and services that reach ``utils.emails`` in the web process. The
task would then exist on the publisher and be unknown to the consumer, which on
the Redis transport is a job that is accepted, delivered, and dropped with a
``NotRegistered`` the publisher never sees.

Importing it here makes the module load during autodiscovery, which is all the
decorator needs. The task itself stays next to ``send_email`` in
``utils/emails.py``, where the retry and masking policy it wraps lives — moving
it into this app would split one email policy across two files.

Accounts is the right home because the OTP mails (signup, login, password
reset) are this app's and are the ones whose loss actually breaks a person's
access.
"""

import logging

from celery import shared_task
from django.core.management import call_command

from utils.emails import send_email_task

logger = logging.getLogger(__name__)

# Explicit re-export: the email import above is load-bearing on its own, and
# without this a linter prunes it as unused and silently unregisters the task.
__all__ = [
    "send_email_task",
    "purge_deleted_accounts",
    "downgrade_precise_locations",
]


@shared_task(name="accounts.purge_deleted_accounts", acks_late=True)
def purge_deleted_accounts():
    """
    Permanently purge accounts whose 30-day deletion window is up.

    Explicit ``name=`` for the reason in core/celery.py: Celery's default is
    the dotted import path, and a task queued under a path-derived name is
    undeliverable after any module move.

    SAFE TO RUN TWICE, which ``acks_late=True`` makes routine. The command
    states it outright — "IDEMPOTENT. A purged row is recognised by its
    tombstone email and skipped" — and each account is purged in its own
    transaction, so a redelivered run skips what finished and picks up what did
    not. Nobody is purged twice and no row is left half-emptied.

    Takes no arguments: which accounts are due is a database question
    (``is_active=False`` AND ``deletion_requested_at`` older than
    ``ACCOUNT_PURGE_AFTER_DAYS``), deliberately not a scheduling one.
    """
    call_command("purge_deleted_accounts")


@shared_task(name="accounts.downgrade_precise_locations", acks_late=True)
def downgrade_precise_locations():
    """
    Pull any profile still pointing at a precise place back to town level.

    A BACKFILL THAT RUNS AS A GUARD, which is worth knowing before reading the
    nightly schedule and expecting work: the picker now searches in city mode
    and the serializer refuses anything that is not a ``city``, so no new
    profile can acquire a ``place`` location. Once this has cleared the rows
    written before those rules, every later run selects zero profiles, makes no
    Google call and writes nothing. It stays scheduled so that a path which
    ever starts writing precise locations again is swept within a day instead
    of indefinitely.

    SAFE TO RUN TWICE — "Safe to run twice" in the command's own words: a
    converted profile points at a ``city`` Location and a nulled one points at
    nothing, so neither is selected again.

    Takes no arguments. The command's ``--dry-run`` is for a human at a shell.
    """
    call_command("downgrade_precise_locations")
