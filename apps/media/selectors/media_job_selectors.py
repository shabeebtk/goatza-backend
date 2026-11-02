"""Reads of MediaJob — always scoped to the owning actor for the API."""

from apps.media.models import MediaJob


def owner_filter(actor) -> dict:
    """
    The owner half of a query for this actor — MediaJob and
    PendingPublication share the owner_user / owner_org pair.
    """
    if actor.is_org:
        return {"owner_org_id": actor.organization.id}
    return {"owner_user_id": actor.user.id}


def job_for_owner(actor, job_id):
    """The actor's own job, or None — for anyone else too (a 404, never 403)."""
    return MediaJob.objects.filter(id=job_id, **owner_filter(actor)).first()


def job_for_source_key(actor, source_key):
    """The actor's job for this source, or None."""
    return MediaJob.objects.filter(
        source_key=source_key, **owner_filter(actor)
    ).first()
