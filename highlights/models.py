from django.db import models
from django.db.models import Q
from accounts.models import User
from organization.models import Organization
from shared.models import BaseUUIDModel


class Highlight(BaseUUIDModel):
    """
    A short video clip curated on a player's profile.

    Media is either uploaded straight here (direct upload) or copied from one of
    the player's own video posts (promotion). Promotion copies the media fields
    outright, so `source_post` is attribution only — deleting the post leaves the
    highlight intact (hence SET_NULL).
    """

    class Visibility(models.TextChoices):
        EVERYONE = "everyone", "Everyone"
        FOLLOWERS_AND_RECRUITERS = "followers_and_recruiters", "Followers and recruiters"
        RECRUITERS_ONLY = "recruiters_only", "Recruiters only"

    # OWNER (players only — enforced in the service layer)
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="highlights"
    )

    title = models.CharField(max_length=80, blank=True)

    # MEDIA (Cloudinary — copied from PostMedia when promoted)
    #
    # 500, not URLField's default 200: a Cloudinary public_id is an actor-scoped
    # path of UUIDs (~120 chars), and the poster URL adds the
    # so_0,f_jpg,q_auto,c_fill,w_360,h_640 transform on top — that lands at ~215
    # and used to fail the insert outright.
    file_url = models.URLField(max_length=500)
    public_id = models.CharField(max_length=255)
    thumbnail_url = models.URLField(max_length=500, blank=True)
    duration = models.PositiveIntegerField(null=True, blank=True)  # seconds
    width = models.PositiveIntegerField(null=True, blank=True)
    height = models.PositiveIntegerField(null=True, blank=True)

    visibility = models.CharField(
        max_length=30,
        choices=Visibility.choices,
        default=Visibility.FOLLOWERS_AND_RECRUITERS
    )

    # Manual ordering (drag to reorder)
    order = models.PositiveIntegerField(default=0)

    # Attribution only — never a dependency for the media itself
    source_post = models.ForeignKey(
        "posts.Post",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="promoted_highlights"
    )

    # Denormalized count
    views_count = models.PositiveIntegerField(default=0)

    is_deleted = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "highlights"
        indexes = [
            models.Index(fields=["user", "is_deleted"]),
            models.Index(fields=["user", "order"]),
        ]

    def __str__(self):
        return f"Highlight {self.title or self.id} - {self.user_id}"


class HighlightView(BaseUUIDModel):
    """
    One watch of a clip, by one actor, on one calendar day.

    Backs "Seen by N recruiters" on the owner's manage screen. Deliberately NOT
    the same number as ``Highlight.views_count``: that counter is every play,
    while these rows are deduplicated per viewer per day so a recruiter who
    rewatches a reel five times still counts once. ``is_recruiter`` is stamped
    at write time — it is a property of the viewer *then*, and a scout who later
    changes role must not rewrite last week's numbers.
    """

    highlight = models.ForeignKey(
        Highlight,
        on_delete=models.CASCADE,
        related_name="views"
    )

    # WHO watched (dual actor — exactly one, like connections.Follow)
    viewer_user = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="highlight_views"
    )
    viewer_org = models.ForeignKey(
        Organization,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="highlight_views"
    )

    is_recruiter = models.BooleanField(default=False)

    # The dedup key. A stored date (rather than a function over created_at)
    # keeps the unique constraint plain and indexable. UTC, matching settings.
    viewed_on = models.DateField()

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "highlight_views"

        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(viewer_user__isnull=False, viewer_org__isnull=True) |
                    Q(viewer_user__isnull=True, viewer_org__isnull=False)
                ),
                name="highlight_view_user_or_org"
            ),
            models.UniqueConstraint(
                fields=["highlight", "viewer_user", "viewed_on"],
                condition=Q(viewer_user__isnull=False),
                name="unique_user_highlight_view_per_day"
            ),
            models.UniqueConstraint(
                fields=["highlight", "viewer_org", "viewed_on"],
                condition=Q(viewer_org__isnull=False),
                name="unique_org_highlight_view_per_day"
            ),
        ]

        indexes = [
            models.Index(fields=["highlight", "is_recruiter"]),
            models.Index(fields=["highlight", "viewed_on"]),
        ]

    def __str__(self):
        viewer = (
            f"User {self.viewer_user_id}"
            if self.viewer_user_id
            else f"Org {self.viewer_org_id}"
        )
        return f"{viewer} watched {self.highlight_id} on {self.viewed_on}"
