"""
Whether this deployment can convert a source right now, and the limits it holds
sources to — GET /media/capabilities, and the gate on signing a source and on
POST /media/jobs.

Imports no model and no view on purpose: the upload signature view imports
this module, and the job service imports that view.
"""

from django.conf import settings

from apps.media.constants import MAX_DURATION_SECONDS
from utils.background_jobs import WORKER_STALE, worker_state


def server_processing_available() -> bool:
    """
    The flag is on, Celery is on, and the worker is not known to be dead.

    "unknown" counts as available — the same rule ``enqueue`` uses. A missing
    heartbeat is what every fresh deploy and flushed cache looks like (see
    utils.background_jobs.worker_state); refusing on it would refuse every
    original for a minute after each deploy.
    """
    return bool(
        settings.MEDIA_PROCESSING_ENABLED
        and settings.CELERY_ENABLED
        and worker_state() != WORKER_STALE
    )


def capabilities() -> dict:
    """The GET /media/capabilities payload."""
    return {
        "server_processing": server_processing_available(),
        "limits": {
            "source_video_bytes": settings.MEDIA_SOURCE_MAX_VIDEO_BYTES,
            "source_image_bytes": settings.MEDIA_SOURCE_MAX_IMAGE_BYTES,
            "max_duration_seconds": dict(MAX_DURATION_SECONDS),
        },
    }
