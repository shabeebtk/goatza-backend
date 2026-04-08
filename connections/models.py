from django.db import models
from django.db.models import Q
from django.core.exceptions import ValidationError
from accounts.models import User
from shared.models import BaseUUIDModel
from organization.models import Organization

# Create your models here.


class Follow(BaseUUIDModel):
    # WHO is following
    follower_user = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="following"
    )

    follower_org = models.ForeignKey(
        Organization,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="following"
    )

    # WHOM they follow
    following_user = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="followers"
    )

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
            # Only one follower type
            models.CheckConstraint(
                condition=(
                    Q(follower_user__isnull=False, follower_org__isnull=True) |
                    Q(follower_user__isnull=True, follower_org__isnull=False)
                ),
                name="follower_user_or_org"
            ),
            # Only one following target
            models.CheckConstraint(
                condition=(
                    Q(following_user__isnull=False, following_org__isnull=True) |
                    Q(following_user__isnull=True, following_org__isnull=False)
                ),
                name="following_user_or_org"
            ),
            models.UniqueConstraint(
                fields=["follower_user", "following_user"],
                name="unique_user_follow"
            ),
            models.UniqueConstraint(
                fields=["follower_org", "following_org"],
                name="unique_org_follow"
            )
        ]
        indexes = [
            models.Index(fields=["follower_user"]),
            models.Index(fields=["follower_org"]),
            models.Index(fields=["following_user"]),
            models.Index(fields=["following_org"]),
        ]

 
    def clean(self):
        # Ensure follower exists
        if not self.follower_user and not self.follower_org:
            raise ValidationError("Follower must be either a user or an organization.")

        # Ensure following exists
        if not self.following_user and not self.following_org:
            raise ValidationError("Following target must be either a user or an organization.")

        # Prevent user -> same user
        if self.follower_user and self.following_user:
            if self.follower_user_id == self.following_user_id:
                raise ValidationError("Users cannot follow themselves.")

        # Prevent org -> same org
        if self.follower_org and self.following_org:
            if self.follower_org_id == self.following_org_id:
                raise ValidationError("Organizations cannot follow themselves.")

    def __str__(self):
        follower = (
            f"User {self.follower_user_id}"
            if self.follower_user
            else f"Org {self.follower_org_id}"
        )

        following = (
            f"User {self.following_user_id}"
            if self.following_user
            else f"Org {self.following_org_id}"
        )

        return f"{follower} -> {following}"