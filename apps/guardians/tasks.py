# guardians/tasks.py
"""
Celery tasks for guardians.

THIN, per CLAUDE.md ("Background jobs"): a ``call_command`` of the management
command that already exists, so the same code runs whether beat triggers it or
somebody types it into a shell. The command keeps its ``--dry-run`` flag and
remains the way to run it by hand.
"""

import logging

from celery import shared_task
from django.core.management import call_command

logger = logging.getLogger(__name__)


@shared_task(name="guardians.purge_unconsented", acks_late=True)
def purge_unconsented():
    """
    Delete minor accounts whose guardian never approved them.

    Explicit ``name=`` for the reason in core/celery.py: Celery's default is
    the dotted import path, and a task queued under a path-derived name is
    undeliverable after any module move.

    SAFE TO RUN TWICE, which ``acks_late=True`` makes routine rather than
    exceptional. The command says so itself \u2014 "IDEMPOTENT. A purged row is
    recognised by its tombstone email and skipped" \u2014 and ``_expire_and_purge``
    wraps the expired event, the token blacklisting and the purge in ONE
    transaction, so a redelivery finds each account either fully swept (and
    skips it) or wholly untouched (and sweeps it). There is no state in which
    a child's account is emptied with no event saying why.

    Takes no arguments: which accounts are due is a database question \u2014 status
    ``pending`` with a newest open request older than
    ``GUARDIAN_PURGE_AFTER_DAYS`` \u2014 deliberately not a scheduling one.

    Scheduled 15 minutes after ``accounts.purge_deleted_accounts`` rather than
    alongside it: this command calls that one's ``_purge`` directly, and
    running them at the same minute would have two processes anonymizing rows
    through the same code path for no benefit.
    """
    call_command("purge_unconsented")
