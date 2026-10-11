# shared/tasks.py
"""
Storage cleanup tasks \u2014 the deletes that used to run inline after commit.

WHY THEY LIVE IN ``shared``: five apps delete R2 objects (posts, recruitments,
matches, accounts, organization) and the work is identical in all five. ``shared``
is an installed app (INSTALLED_APPS, next to ``apps.*``), so
``app.autodiscover_tasks()`` picks this module up; putting it under any one of
those apps would make the other four import across an app boundary to reach it.

WHY THEY ARE TASKS AT ALL. Every one of those call sites was already deferred
to ``transaction.on_commit`` and labelled best-effort, which was right about
the ordering and wrong about the cost: an on_commit callback runs in the
REQUEST's thread, after the response status is decided but before the worker is
free, so a slow R2 (or a 30-second connect timeout to it) was time the user
spent waiting on a file they had already been told was deleted. And
"best-effort" meant the object was simply orphaned \u2014 billed forever, with one
log line as its gravestone.

THE RETRY POLICY IS THE OPPOSITE OF THE PUSH AND EMAIL TASKS, deliberately.
Those are at-most-once (``acks_late=False``, no retries) because sending twice
is worse than not sending. A delete is IDEMPOTENT \u2014 deleting a key that is
already gone is a no-op on S3/R2, not an error \u2014 so these are at-least-once:
``acks_late=True`` (the project default), and a failure means "later", never
"never". Backoff is capped at 10 minutes so a long R2 incident is ridden out
rather than hammered.
"""

import logging

from celery import shared_task

from services.storage.factory import get_storage_service

logger = logging.getLogger(__name__)

# One policy, two tasks. Kept in a dict so they cannot drift apart: the two do
# the same job (remove objects nobody references) and differ only in whether
# the caller knows the keys or only the prefix.
#
# HOW FAR ``autoretry_for`` ACTUALLY REACHES, so the next reader is not misled:
# ``R2Service.delete_file`` and ``delete_folder_data`` catch ``Exception``
# themselves and log at ERROR (services/storage/r2.py), which is their
# documented contract and is NOT changed here. So a genuine R2 blip is absorbed
# inside the storage service and never reaches this retry \u2014 what retries is a
# failure getting a storage service at all (a bad FILE_STORAGE_PROVIDER, boto3
# client construction, missing credentials). Making an R2 timeout retryable
# needs a raising variant in the storage service; that is a change to its API
# and belongs with whoever owns it.
_DELETE_POLICY = {
    "acks_late": True,
    "autoretry_for": (Exception,),
    "retry_backoff": True,
    "retry_backoff_max": 600,
    "max_retries": 5,
    "bind": True,
}


def _giving_up(task):
    """True on the attempt that will not be retried again."""
    # request.retries is the number of retries ALREADY made, so on the last
    # allowed attempt it has reached max_retries.
    return task.request.retries >= task.max_retries


@shared_task(name="storage.delete_keys", **_DELETE_POLICY)
def delete_keys(self, keys):
    """
    Delete specific stored objects by key.

    ``keys`` is a list of plain strings (the ``*_public_id`` columns) \u2014 JSON,
    like every task argument. An empty list is a no-op, which is the normal
    case for a caller that found nothing orphaned.

    SAFE TO RUN TWICE, which ``acks_late=True`` makes routine rather than
    exceptional: the second run deletes keys that are already gone, and R2
    answers that without complaint.
    """
    keys = [key for key in (keys or []) if key]

    if not keys:
        logger.info("storage.delete_keys | nothing to delete")
        return

    try:
        storage = get_storage_service()

        for key in keys:
            storage.delete_file(key)
    except Exception:
        if _giving_up(self):
            # THE ORPHAN'S LAST TRACE. ERROR so Sentry raises an event, and
            # with every key spelled out: nothing else in the system knows
            # these objects exist any more \u2014 the rows that referenced them are
            # gone \u2014 so this line is the only way anybody could find and
            # remove them by hand.
            logger.error(
                "storage.delete_keys | GIVING UP after %s retries, objects are "
                "orphaned | keys=%s",
                self.max_retries, keys, exc_info=True,
            )
        raise

    logger.info("storage.delete_keys | done | count=%s", len(keys))


@shared_task(name="storage.delete_folder", **_DELETE_POLICY)
def delete_folder(self, folder_path):
    """
    Delete every object under a key prefix (a post's media folder).

    R2 has no folders \u2014 a folder is a shared prefix \u2014 so the storage service
    lists and deletes in pages. An empty or already-swept prefix is a no-op:
    the caller has no way to know whether anything was ever uploaded there.

    SAFE TO RUN TWICE for the same reason as ``delete_keys``: a redelivered
    sweep finds an empty prefix.
    """
    if not folder_path:
        logger.info("storage.delete_folder | no prefix given, nothing to do")
        return

    try:
        storage = get_storage_service()
        storage.delete_folder_data(folder_path)
    except Exception:
        if _giving_up(self):
            logger.error(
                "storage.delete_folder | GIVING UP after %s retries, objects "
                "under this prefix are orphaned | prefix=%s",
                self.max_retries, folder_path, exc_info=True,
            )
        raise

    logger.info("storage.delete_folder | done | prefix=%s", folder_path)
