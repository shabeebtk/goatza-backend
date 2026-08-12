"""
Impression ingest (§3.2 server side).

Fire-and-forget from the reader's point of view: nothing in here may raise into
a response, and nothing may be slow enough to be felt while scrolling.
"""

import logging
import random
import uuid
from datetime import timedelta

from django.db.models import F
from django.utils import timezone

from feed.models import PostImpression
from posts.models import Post

logger = logging.getLogger(__name__)

# Hard cap per request — the client buffers ~10 at a time, so anything near
# this is either a very long background flush or someone probing the endpoint.
MAX_IMPRESSIONS_PER_REQUEST = 100

# The spec's nightly cleanup, rewritten as a piggyback: there is no scheduler in
# this deployment. Scoped to one user and hitting the (user, last_seen_at)
# index, it is cheap enough to sit in a hot path, and at ~1 in 50 flushes it
# keeps the table bounded without every reader paying for it.
IMPRESSION_RETENTION_DAYS = 30
PRUNE_PROBABILITY = 0.02


class FeedImpressionService:

    @staticmethod
    def parse_post_ids(raw_ids):
        """
        Valid, de-duplicated, capped post ids from an untrusted list.

        Junk is dropped silently rather than rejected: this is telemetry, and a
        400 here would surface an error toast over something the reader did not
        ask for and cannot act on.
        """
        if not isinstance(raw_ids, (list, tuple)):
            return []

        post_ids, seen = [], set()
        for raw in raw_ids[:MAX_IMPRESSIONS_PER_REQUEST * 2]:
            try:
                post_id = uuid.UUID(str(raw).strip())
            except (ValueError, AttributeError, TypeError):
                continue
            if post_id in seen:
                continue
            seen.add(post_id)
            post_ids.append(post_id)
            if len(post_ids) >= MAX_IMPRESSIONS_PER_REQUEST:
                break

        return post_ids

    @staticmethod
    def record(user, post_ids):
        """
        Upsert one flush: bump ``seen_count``, refresh ``last_seen_at``.

        UPDATE-then-INSERT rather than the single ``update_conflicts`` upsert:
        Django's ON CONFLICT ... DO UPDATE assigns the EXCLUDED value, so it
        would reset seen_count to 1 on every flush instead of counting. Doing
        the increment first and letting the insert ignore conflicts gets a real
        count with no read-then-write race — concurrent flushes either both
        increment, or one inserts and the other's insert is discarded.

        Returns the number of ids actually attributed.
        """
        if not post_ids:
            return 0

        # A client can send a well-formed uuid for a post that no longer
        # exists; inserting that FK would be an IntegrityError on a path that
        # must never fail.
        live_ids = list(
            Post.objects
            .filter(id__in=post_ids, is_deleted=False)
            .values_list("id", flat=True)
        )
        if not live_ids:
            return 0

        now = timezone.now()

        PostImpression.objects.filter(user=user, post_id__in=live_ids).update(
            seen_count=F("seen_count") + 1,
            last_seen_at=now,
        )

        PostImpression.objects.bulk_create(
            [
                PostImpression(
                    user=user, post_id=post_id, last_seen_at=now, seen_count=1
                )
                for post_id in live_ids
            ],
            ignore_conflicts=True,
        )

        return len(live_ids)

    @staticmethod
    def maybe_prune(user):
        """Opportunistic retention sweep — see PRUNE_PROBABILITY."""
        if random.random() >= PRUNE_PROBABILITY:
            return

        try:
            PostImpression.objects.filter(
                user=user,
                last_seen_at__lt=timezone.now() - timedelta(
                    days=IMPRESSION_RETENTION_DAYS
                ),
            ).delete()
        except Exception as exc:
            # Housekeeping must never take the write path down with it.
            logger.warning("FeedImpressionService | prune failed | %s", exc)
