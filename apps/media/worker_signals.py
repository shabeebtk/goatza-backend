"""
What the MEDIA worker does when it starts — Celery's ``worker_ready``.

Connected in MediaConfig.ready(), so it is imported in every process (web,
both worker processes); it ACTS only in a worker that consumes the "media"
queue (CELERY_TASK_ROUTES; scripts/worker-entrypoint.sh's `-Q media`). The
default worker, the web service and a test run never touch a job here.

WHY. Every Render deploy SIGTERMs, then SIGKILLs, the worker mid-encode, and
an OOM restart does the same without warning. The job is left `processing`:
the sweeper would only call it dead ~32 minutes later (MEDIA_JOB_TIME_LIMIT +
5 min) and charge it another attempt, and a redelivered task is dropped by
MediaJobService.claim as "owned by a live run". At startup nothing can be
converting yet, so MediaSweepService.recover_after_restart requeues every
`processing` job (attempts unchanged), dispatches it again and deletes the
temp folders a hard-killed run leaves behind.

SAFE ONLY BECAUSE EXACTLY ONE MEDIA WORKER EXISTS — one container, one `-Q
media` process. A second one starting would requeue jobs the first is still
converting: no data lost (claim and the saved output keys make a rerun
overwrite the same objects) but a whole encode wasted. Scale media with
concurrency inside that one process, or give this a per-worker claim first.
"""

import logging

from celery.signals import worker_ready

from apps.media.services.media_sweep_service import MediaSweepService

logger = logging.getLogger(__name__)

MEDIA_QUEUE = "media"


def consumes_media_queue(consumer) -> bool:
    """Does the worker that sent ``worker_ready`` consume the media queue?"""
    task_consumer = getattr(consumer, "task_consumer", None)
    queues = getattr(task_consumer, "queues", None) or ()
    return any(queue.name == MEDIA_QUEUE for queue in queues)


@worker_ready.connect(dispatch_uid="media.recover_after_restart")
def recover_after_restart(sender=None, **kwargs):
    if not consumes_media_queue(sender):
        return

    try:
        MediaSweepService.recover_after_restart()
    except Exception:
        # The worker must start regardless: the sweeper's dead-run branch
        # still recovers these jobs, only later.
        logger.error(
            "media.worker_ready | startup recovery failed", exc_info=True
        )
