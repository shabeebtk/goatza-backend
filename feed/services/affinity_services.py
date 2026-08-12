"""
Affinity writes (§3.6) — personalization without ML and without a scheduler.

The spec runs a nightly Celery job that re-aggregates 30 days of likes,
comments and messages. There is no worker here, so the same curve is produced
incrementally: every interaction decays the stored score to *now* and then adds
its weight, and the ranker decays again from ``updated_at`` when it reads.

This puts one indexed upsert on three hot paths. At current volume that is
negligible; if it ever isn't, this is the first thing that should move behind a
queue — which is why it is a service call and not inline SQL at each site.
"""

import logging

from django.db import IntegrityError
from django.db.models import (
    DurationField, ExpressionWrapper, F, FloatField, Value,
)
from django.db.models.functions import Extract, Now, Power

from feed.models import ActorAffinity
from feed.selectors.feed_selectors import AFFINITY_HALF_LIFE_DAYS

logger = logging.getLogger(__name__)


class AffinityService:
    """§3.6 interaction weights."""

    LIKE = 2.0
    COMMENT = 3.0
    MESSAGE = 5.0

    @classmethod
    def record(cls, viewer, delta, author_user_id=None, author_org_id=None):
        """
        Add ``delta`` to (viewer → author), decaying whatever is stored first.

        Takes ids, not instances: every call site already holds the author's id
        on the row it just wrote, and asking for the object would put an extra
        query on a hot path to feed a fire-and-forget signal.

        Best-effort by design — a failure here must never fail the like, comment
        or message that triggered it, so everything is swallowed and logged.
        """
        if not viewer or delta <= 0:
            return
        if not author_user_id and not author_org_id:
            return
        # Engaging with your own content says nothing about who you want to see.
        if author_user_id and str(author_user_id) == str(viewer.pk):
            return

        target = {"author_user_id": author_user_id, "author_org_id": author_org_id}

        try:
            updated = cls._decay_and_add(viewer, target, delta)
            if updated:
                return

            try:
                ActorAffinity.objects.create(viewer=viewer, score=delta, **target)
            except IntegrityError:
                # Lost the create race — the row exists now, so apply the delta
                # the same way the first branch would have.
                cls._decay_and_add(viewer, target, delta)

        except Exception as exc:
            logger.warning("AffinityService | record failed | %s", exc)

    @staticmethod
    def _decay_and_add(viewer, target, delta):
        """
        One statement: ``score = score * 0.5 ** (age_days / 30) + delta``.

        The decay is computed from the row's OWN ``updated_at`` inside the
        UPDATE, so no read is needed and two concurrent writers cannot lose each
        other's delta. ``updated_at`` is set explicitly because auto_now does not
        fire on a queryset update.
        """
        age_days = ExpressionWrapper(
            Extract(
                ExpressionWrapper(Now() - F("updated_at"), output_field=DurationField()),
                "epoch",
            ) / Value(86400.0),
            output_field=FloatField(),
        )

        decayed = ExpressionWrapper(
            F("score") * Power(
                Value(0.5),
                ExpressionWrapper(
                    age_days / Value(AFFINITY_HALF_LIFE_DAYS),
                    output_field=FloatField(),
                ),
            ),
            output_field=FloatField(),
        )

        return ActorAffinity.objects.filter(viewer=viewer, **target).update(
            score=ExpressionWrapper(
                decayed + Value(delta), output_field=FloatField()
            ),
            updated_at=Now(),
        )

    # ------------------------------------------------------------------ #
    # CALL-SITE HELPERS
    # ------------------------------------------------------------------ #
    @classmethod
    def record_for_actor(cls, actor, delta, author_user_id=None, author_org_id=None):
        """
        Same as ``record`` but takes the acting identity.

        Affinity is stored per PERSON (viewer is a User), so acting as an org
        still credits the human behind it — the same rule impressions follow.
        """
        if actor is None:
            return

        viewer = actor.user if actor.is_user else getattr(
            actor.organization_member, "user", None
        )
        cls.record(
            viewer,
            delta,
            author_user_id=author_user_id,
            author_org_id=author_org_id,
        )
