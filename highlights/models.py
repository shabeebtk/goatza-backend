from django.db import models
from accounts.models import User
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
    file_url = models.URLField()
    public_id = models.CharField(max_length=255)
    thumbnail_url = models.URLField(blank=True)
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
