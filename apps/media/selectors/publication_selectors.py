"""Reads of PendingPublication."""

from datetime import timedelta

from django.utils import timezone

from apps.media.constants import PUBLICATION_LIST_DAYS, PUBLICATION_LIST_LIMIT
from apps.media.models import PendingPublication
from apps.media.selectors.media_job_selectors import owner_filter


def publications_for(actor, statuses):
    """
    The actor's own, not dismissed, from the last week, newest first, at most
    PUBLICATION_LIST_LIMIT — what a client restores its "Posting…" bar from
    after a reload. A week, because a failure nobody has seen in that long is
    no longer worth a card.
    """
    since = timezone.now() - timedelta(days=PUBLICATION_LIST_DAYS)
    return list(
        PendingPublication.objects
        .filter(
            status__in=statuses,
            dismissed_at__isnull=True,
            created_at__gte=since,
            **owner_filter(actor),
        )
        .prefetch_related("jobs")
        .order_by("-created_at")[:PUBLICATION_LIST_LIMIT]
    )
