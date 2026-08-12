from django.db import models
from django.db.models import Q

from shared.models import BaseUUIDModel
from accounts.models import User
from organization.models import Organization
from posts.models import Post


class PostImpression(BaseUUIDModel):
    """
    What a PERSON has already been shown in a feed (§3.2).

    Keyed to the user, not the acting identity: someone who read a post while
    browsing as their club has read it, so re-serving it after they switch back
    to their personal actor is exactly the repetition the seen-penalty exists to
    remove. The feed is actor-aware; memory of having read something is not.

    Retention is an opportunistic sweep on the write endpoint rather than the
    nightly job the spec assumes — there is no Celery worker in this deployment
    (see feed.services.impression_services).
    """

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="post_impressions",
    )
    post = models.ForeignKey(
        Post,
        on_delete=models.CASCADE,
        related_name="impressions",
    )
    last_seen_at = models.DateTimeField()
    seen_count = models.PositiveIntegerField(default=1)

    class Meta:
        db_table = "post_impressions"
        constraints = [
            # Unconditional (both columns are NOT NULL), which is also what lets
            # the write path lean on ON CONFLICT for a race-free upsert.
            models.UniqueConstraint(
                fields=["user", "post"],
                name="unique_user_post_impression",
            ),
        ]
        indexes = [
            # Serves both hot paths: the rank-time lookup (viewer + candidate
            # ids) and the retention sweep (viewer + age).
            models.Index(fields=["user", "last_seen_at"]),
        ]

    def __str__(self):
        return f"{self.user_id} saw {self.post_id} x{self.seen_count}"


class ActorAffinity(BaseUUIDModel):
    """
    How much a viewer actually engages with one author (§3.6).

    The spec recomputes this nightly. With no worker, the same numbers are
    produced by writing incrementally and decaying at BOTH ends: the stored
    score is decayed to "now" before each delta is added, and decayed again from
    ``updated_at`` when the ranker reads it. A 30-day half-life applied twice
    over disjoint intervals is the same curve as one applied over the whole
    span, so the table is self-maintaining.

    ``viewer`` is a User, not an actor: affinity is a property of the person,
    for the same reason impressions are.
    """

    viewer = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="actor_affinities",
    )
    author_user = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="affinity_as_author",
    )
    author_org = models.ForeignKey(
        Organization,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="affinity_as_author",
    )
    score = models.FloatField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "actor_affinities"
        constraints = [
            # Exactly one author — same shape as Notification's actor columns.
            models.CheckConstraint(
                condition=(
                    Q(author_user__isnull=False, author_org__isnull=True) |
                    Q(author_user__isnull=True, author_org__isnull=False)
                ),
                name="affinity_author_user_or_org",
            ),
            # Two PARTIAL uniques rather than one unique on all three columns:
            # NULL never equals NULL in SQL, so the three-column version would
            # happily store the same (viewer, author) pair twice. Same reasoning
            # as PostMention / SavedPost.
            models.UniqueConstraint(
                fields=["viewer", "author_user"],
                condition=Q(author_user__isnull=False),
                name="unique_affinity_viewer_author_user",
            ),
            models.UniqueConstraint(
                fields=["viewer", "author_org"],
                condition=Q(author_org__isnull=False),
                name="unique_affinity_viewer_author_org",
            ),
        ]
        indexes = [
            # The ranker loads every affinity a viewer holds in one query.
            models.Index(fields=["viewer"]),
        ]

    def __str__(self):
        target = self.author_user_id or f"org_{self.author_org_id}"
        return f"{self.viewer_id} -> {target} = {self.score}"
