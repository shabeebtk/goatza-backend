from rest_framework import serializers

from apps.media.constants import error_message
from apps.media.models import MediaJob


class MediaJobSerializer(serializers.Serializer):
    """
    The ONE job payload — the REST responses and the ``media_job_updated``
    websocket event both send exactly this, so the client has one schema.

        {"id", "status", "kind", "purpose", "error_code", "error_message"}
        + when ready: {"file_url", "public_id", "thumbnail_url", "width",
                       "height", "duration", "size_bytes", "media_type"}

    The ready keys are named like the attach endpoints' fields, so a client
    hands them on as if it had uploaded the file itself. ``error_message`` is
    the human sentence for ``error_code``; ``error_detail`` never leaves the
    server. Primitives only: the channel layer has to msgpack this.
    """

    def to_representation(self, job):
        payload = {
            "id": str(job.id),
            "status": job.status,
            "kind": job.kind,
            "purpose": job.purpose,
            "error_code": job.error_code or None,
            "error_message": error_message(job.error_code, job.purpose),
        }

        if job.status == MediaJob.Status.READY:
            payload.update({
                "file_url": job.output_url,
                "public_id": job.output_key,
                "thumbnail_url": job.thumbnail_url,
                "width": job.width,
                "height": job.height,
                "duration": job.duration,
                "size_bytes": job.size_bytes,
                "media_type": job.kind,
            })

        return payload
