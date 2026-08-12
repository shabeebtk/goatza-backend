"""
Read queries for the ranking pipeline.

Both of these run once per ranking build, over the whole candidate window —
never per post. A per-candidate lookup here is how a 300-row rerank turns into
300 queries.
"""

from django.utils import timezone

from feed.models import ActorAffinity, PostImpression
from feed.services.feed_services import AFFINITY_CAP


# §3.6 — the half-life the stored score is decayed with, at write AND at read.
AFFINITY_HALF_LIFE_DAYS = 30.0


def impressions_for(viewer, post_ids):
    """
    ``{post_id: last_seen_at}`` for the candidate window.

    Keyed on the person, not the acting identity — see PostImpression.
    """
    if not viewer or not post_ids:
        return {}

    return dict(
        PostImpression.objects
        .filter(user=viewer, post_id__in=post_ids)
        .values_list("post_id", "last_seen_at")
    )


def affinities_for(viewer):
    """
    ``{author_key: boost}`` for every author this viewer has engaged with,
    decayed from ``updated_at`` to now and clamped to the §6 cap.

    The decay is applied here rather than by a nightly job: a 30-day half-life
    applied at write time (up to ``updated_at``) and again at read time (from
    ``updated_at`` to now) covers the same span as one continuous decay, so the
    stored number never needs recomputing.

    ``author_key`` matches ranking_services.author_key — a club and a person
    are different authors even when the same human runs both.
    """
    if not viewer:
        return {}

    now = timezone.now()
    affinities = {}

    rows = ActorAffinity.objects.filter(viewer=viewer).values(
        "author_user_id", "author_org_id", "score", "updated_at"
    )

    for row in rows:
        age_days = max(
            (now - row["updated_at"]).total_seconds() / 86400.0,
            0.0,
        )
        decayed = row["score"] * (0.5 ** (age_days / AFFINITY_HALF_LIFE_DAYS))
        if decayed <= 0:
            continue

        if row["author_user_id"]:
            key = str(row["author_user_id"])
        else:
            key = f"org_{row['author_org_id']}"

        affinities[key] = min(decayed, AFFINITY_CAP)

    return affinities
