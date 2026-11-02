"""
One server-side conversion of an uploaded original ("source") into the ready
files the browser would otherwise have made.

A job is created by POST /media/jobs once the source is in the bucket, worked
by ``media.process_job`` on the worker, and finished as ``ready`` (output +
thumbnail keys filled in) or ``failed`` (an ``error_code`` from
apps/media/constants.py). Nothing attaches a job to a post or a message here:
the client uses the job's output exactly like a file it uploaded itself.
"""

from django.db import models
from django.db.models import Q

from apps.accounts.models import User
from apps.media.constants import (
    KIND_IMAGE,
    KIND_VIDEO,
    PURPOSES,
    TARGET_HIGHLIGHT,
    TARGET_POST,
)
from apps.organization.models import Organization
from shared.models import BaseUUIDModel


class MediaJob(BaseUUIDModel):

    class Kind(models.TextChoices):
        IMAGE = KIND_IMAGE, "Image"
        VIDEO = KIND_VIDEO, "Video"

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        PROCESSING = "processing", "Processing"
        READY = "ready", "Ready"
        FAILED = "failed", "Failed"

    # The actor the files belong to — exactly one, like every dual-actor row.
    owner_user = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="media_jobs"
    )
    owner_org = models.ForeignKey(
        Organization,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="media_jobs"
    )
    # The human who uploaded. An org actor has no user on the owner side.
    created_by = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="media_jobs_created"
    )

    purpose = models.CharField(
        max_length=20,
        choices=[(purpose, purpose.capitalize()) for purpose in PURPOSES]
    )
    # The POLICY type the source was signed under. Differs from purpose for a
    # highlight, which the client signs as "posts".
    upload_type = models.CharField(max_length=20)
    kind = models.CharField(max_length=10, choices=Kind.choices)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.QUEUED,
        db_index=True
    )

    # Unique: the key IS the job's identity, which is what makes POST
    # /media/jobs idempotent.
    source_key = models.CharField(max_length=255, unique=True)
    source_content_type = models.CharField(max_length=100)
    source_size_bytes = models.PositiveBigIntegerField()

    # Decided ONCE and saved before anything is uploaded, so a rerun writes
    # the same objects instead of orphaning a second set.
    output_key = models.CharField(max_length=255, blank=True)
    output_url = models.URLField(max_length=500, blank=True)
    thumbnail_key = models.CharField(max_length=255, blank=True)
    thumbnail_url = models.URLField(max_length=500, blank=True)

    # Measured from the output by the worker — unlike every other media row,
    # these are not client-reported.
    width = models.PositiveIntegerField(null=True, blank=True)
    height = models.PositiveIntegerField(null=True, blank=True)
    duration = models.PositiveIntegerField(null=True, blank=True)  # seconds
    size_bytes = models.PositiveBigIntegerField(null=True, blank=True)

    error_code = models.CharField(max_length=40, blank=True)
    # INTERNAL ONLY — ffmpeg's stderr, a traceback. Never sent to a client.
    error_detail = models.TextField(blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)

    # When the source object's delete was dispatched (ready: right away;
    # failed: by the sweeper after a day). Lets the sweeper skip jobs it has
    # already cleaned instead of asking R2 about every old failure forever.
    source_deleted_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "media_jobs"
        indexes = [
            models.Index(fields=["status", "created_at"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(owner_user__isnull=False, owner_org__isnull=True) |
                    Q(owner_user__isnull=True, owner_org__isnull=False)
                ),
                name="media_job_owner_user_or_org"
            )
        ]

    def __str__(self):
        return f"MediaJob {self.id} ({self.kind}, {self.status})"


class PendingPublication(BaseUUIDModel):
    """
    A post or highlight the author has already submitted, waiting for its
    media to finish converting — published BY THE SERVER when it does, whether
    or not the author still has the app open.

    THE POST DOES NOT EXIST YET, and that is the design. The Post / Highlight
    row is created only once every job is ready, through exactly the create
    path a browser-encoded upload takes — so no read path anywhere (feeds,
    profiles, search, saved, mentions, shares, counters, notifications) needs
    to know about "processing", and mentions, hashtags and counters fire the
    way they always do. This row is what holds the request meanwhile.

    PublicationService.try_publish is the only thing that turns one into a
    real post.
    """

    class Target(models.TextChoices):
        POST = TARGET_POST, "Post"
        HIGHLIGHT = TARGET_HIGHLIGHT, "Highlight"

    class Status(models.TextChoices):
        PROCESSING = "processing", "Processing"
        PUBLISHED = "published", "Published"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    owner_user = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="pending_publications"
    )
    owner_org = models.ForeignKey(
        Organization,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="pending_publications"
    )
    # The human who submitted. For an org post it is who the org actor is
    # rebuilt from at publish time — and they must still be a member then.
    created_by = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="pending_publications_created"
    )

    target = models.CharField(max_length=20, choices=Target.choices)
    # The VALIDATED request body, media items as written (job ids and any
    # ready items), so publishing re-runs the normal create path on it.
    payload = models.JSONField(default=dict)
    jobs = models.ManyToManyField(MediaJob, related_name="publications")

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PROCESSING,
        db_index=True
    )
    # The Post / Highlight id once published.
    result_id = models.UUIDField(null=True, blank=True)
    error_code = models.CharField(max_length=40, blank=True)
    # The human sentence the author is shown — unlike MediaJob.error_detail,
    # this one is meant for them.
    error_message = models.CharField(max_length=500, blank=True)

    # The author closed the failure card; it stops coming back on reload.
    dismissed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    published_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "media_pending_publications"
        indexes = [
            models.Index(fields=["status", "created_at"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(owner_user__isnull=False, owner_org__isnull=True) |
                    Q(owner_user__isnull=True, owner_org__isnull=False)
                ),
                name="pending_publication_owner_user_or_org"
            )
        ]

    def __str__(self):
        return f"PendingPublication {self.id} ({self.target}, {self.status})"
