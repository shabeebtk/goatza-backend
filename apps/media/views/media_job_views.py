"""
HTTP entry points for server-side media processing, under /media/.

Thin: read the body, hand it to MediaJobService or a selector, map the outcome
onto the standard envelope. Refusals use the upload endpoints' own
``upload_error`` — this is the last step of an upload, and the client already
parses that shape (``code`` included).

The create view owns its 429 for the same reason support's report view does:
throttling fires in DRF's ``initial()``, before ``post()`` runs, so only
``handle_exception`` can keep it in the standard envelope.
"""

import logging

from rest_framework.exceptions import Throttled

from apps.accounts.views.user_upload_signature_views import (
    resolve_upload_actor,
    upload_error,
)
from apps.media.selectors.capability_selectors import capabilities
from apps.media.selectors.media_job_selectors import job_for_owner
from apps.media.serializers.media_job_serializers import MediaJobSerializer
from apps.media.services.media_job_service import MediaJobError, MediaJobService
from apps.media.throttles import MediaJobThrottle
from core.views.base_views import BaseAPIView
from utils.errors import error_body
from utils.response import response_data

logger = logging.getLogger(__name__)

THROTTLED = "Too many files processing. Please try again later."


class MediaCapabilitiesAPIView(BaseAPIView):
    """
    GET /media/capabilities

        {"server_processing": true,
         "limits": {"source_video_bytes", "source_image_bytes",
                    "max_duration_seconds": {"posts": 300, ...}}}

    What the client reads before deciding whether a file it cannot convert
    can go up as a `source` at all.
    """

    def get(self, request):
        return response_data(success=True, data=capabilities())


class MediaJobCreateAPIView(BaseAPIView):
    """
    POST /media/jobs

    Body: ``{"key", "purpose", "upload_type", "org_id"?}`` — ``key`` is the
    `source` key the signature endpoint issued, after the upload finished.

        201 {"job": {...}}   queued
        200 {"job": {...}}   this source already has a job (idempotent)
        400 code server_processing_unavailable | SOURCE_MISSING |
            SOURCE_TOO_LARGE | SOURCE_UNREADABLE, or a field error

    Then watch for ``media_job_updated`` on the notifications socket, or poll
    GET /media/jobs/<id>.
    """

    throttle_classes = [MediaJobThrottle]

    def post(self, request):
        body = request.data if isinstance(request.data, dict) else {}

        try:
            job, created = MediaJobService.create_job(
                request=request,
                actor=self.actor,
                key=body.get("key"),
                purpose=body.get("purpose"),
                upload_type=body.get("upload_type"),
                org_id=body.get("org_id"),
            )
        except MediaJobError as e:
            return upload_error(e.message, e.field, e.status_code, code=e.code)
        except Exception as e:
            logger.error(
                "MediaJobCreateAPIView.post | Error | key=%s",
                body.get("key"), exc_info=True,
            )
            return response_data(
                success=False,
                message="Failed to start processing",
                status_code=500,
                error=str(e),
            )

        return response_data(
            success=True,
            data={"job": MediaJobSerializer(job).data},
            status_code=201 if created else 200,
        )

    def handle_exception(self, exc):
        """Keep the 429 in the standard envelope — see the module docstring."""
        if isinstance(exc, Throttled):
            wait = int(exc.wait or 0)
            logger.warning(
                "MediaJobCreateAPIView | Throttled | retry_after=%s", wait
            )
            return response_data(
                success=False,
                message=THROTTLED,
                status_code=429,
                data={**error_body(THROTTLED), "retry_after": wait},
            )

        return super().handle_exception(exc)


class MediaJobDetailAPIView(BaseAPIView):
    """
    GET /media/jobs/<uuid>?org_id=<uuid>

    The owner's job, or 404 — for a job that does not exist and for somebody
    else's alike, so an id is never confirmed to a stranger. ``org_id`` only
    when the job was created with one (a user uploading for their org).
    """

    def get(self, request, job_id):
        actor, refusal = resolve_upload_actor(
            request, self.actor, None, request.query_params.get("org_id")
        )
        if refusal:
            return upload_error(*refusal)

        job = job_for_owner(actor, job_id)
        if job is None:
            return response_data(
                success=False,
                message="Not found",
                status_code=404,
                data=error_body("Not found"),
            )

        return response_data(
            success=True,
            data={"job": MediaJobSerializer(job).data},
        )
