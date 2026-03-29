from django.db import models
from django.db.models import Q
from django.core.exceptions import ValidationError
from accounts.models import User
from core.models import BaseUUIDModel
from organization.models import Organization

# Create your models here.


class Follow(BaseUUIDModel):
    follower = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="following"
    )
    # Follow a user
    following_user = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="followers"
    )
    # Follow an organization
    following_org = models.ForeignKey(
        Organization,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="followers"
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "follows"
        constraints = [
            # Ensure only one type is followed
            models.CheckConstraint(
                condition=(
                    Q(following_user__isnull=False, following_org__isnull=True) |
                    Q(following_user__isnull=True, following_org__isnull=False)
                ),
                name="follow_user_or_org_only"
            ),
            # Prevent duplicate follows (user → user)
            models.UniqueConstraint(
                fields=["follower", "following_user"],
                name="unique_user_follow"
            ),
            # Prevent duplicate follows (user → org)
            models.UniqueConstraint(
                fields=["follower", "following_org"],
                name="unique_org_follow"
            ),
        ]

        indexes = [
            models.Index(fields=["follower"]),
            models.Index(fields=["following_user"]),
            models.Index(fields=["following_org"]),
            models.Index(fields=["-created_at"]),
        ]

    def clean(self):
        # Prevent self-follow
        if self.following_user and self.follower_id == self.following_user_id:
            raise ValidationError("Users cannot follow themselves.")

    def __str__(self):
        if self.following_user:
            return f"{self.follower_id} -> User {self.following_user_id}"
        return f"{self.follower_id} -> Org {self.following_org_id}"