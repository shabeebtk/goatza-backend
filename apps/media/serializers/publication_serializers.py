from rest_framework import serializers

from apps.media.constants import PUBLICATION_PREVIEW_CHARS
from apps.media.models import MediaJob, PendingPublication

_datetime = serializers.DateTimeField()


def _excerpt(text):
    text = " ".join((text or "").split())
    if len(text) <= PUBLICATION_PREVIEW_CHARS:
        return text
    return text[:PUBLICATION_PREVIEW_CHARS - 1].rstrip() + "…"


class PendingPublicationSerializer(serializers.Serializer):
    """
    The ONE publication payload — GET /media/publications, the 202 from a
    create, and the ``publication_updated`` websocket event all send exactly
    this. Primitives only: the channel layer has to msgpack it.

        {"id", "target", "status", "error_code", "error_message",
         "result_id", "created_at", "published_at",
         "preview": {"media_type", "thumbnail_url", "content"},
         "jobs": [{"id", "status"}]}

    ``preview`` is what the client's "Posting…" bar draws after a reload, when
    it no longer has the files the author picked.
    """

    def to_representation(self, publication):
        jobs = sorted(publication.jobs.all(), key=lambda job: job.created_at)

        return {
            "id": str(publication.id),
            "target": publication.target,
            "status": publication.status,
            "error_code": publication.error_code or None,
            "error_message": publication.error_message or None,
            "result_id": str(publication.result_id)
            if publication.result_id else None,
            "created_at": _datetime.to_representation(publication.created_at),
            "published_at": _datetime.to_representation(
                publication.published_at
            ) if publication.published_at else None,
            "preview": self._preview(publication, jobs),
            "jobs": [{"id": str(job.id), "status": job.status} for job in jobs],
        }

    @staticmethod
    def _preview(publication, jobs):
        payload = publication.payload or {}
        jobs_by_id = {str(job.id): job for job in jobs}

        if publication.target == PendingPublication.Target.HIGHLIGHT:
            items = [payload]
            content = payload.get("title")
        else:
            items = [item for item in payload.get("media") or []
                     if isinstance(item, dict)]
            content = payload.get("content")

        def job_of(item):
            return jobs_by_id.get(str(item.get("media_job_id") or ""))

        media_type = None
        if items:
            first_job = job_of(items[0])
            media_type = first_job.kind if first_job \
                else items[0].get("media_type")

        # The first converted file's poster; failing that, the first file the
        # browser had already made (a gallery can mix the two).
        thumbnail_url = next(
            (job.thumbnail_url for job in map(job_of, items)
             if job and job.status == MediaJob.Status.READY
             and job.thumbnail_url),
            None,
        ) or next(
            (item.get("thumbnail_url") for item in items
             if not job_of(item) and item.get("thumbnail_url")),
            None,
        )

        return {
            "media_type": media_type,
            "thumbnail_url": thumbnail_url,
            "content": _excerpt(content),
        }
