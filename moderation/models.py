from django.db import models
from django.db.models import F, Q
from django.core.exceptions import ValidationError
from accounts.models import User
from shared.models import BaseUUIDModel
from organization.models import Organization

# Create your models here.


class Block(BaseUUIDModel):
    # WHO is blocking
    blocker_user = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="blocks_made"
    )

    blocker_org = models.ForeignKey(
        Organization,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="blocks_made"
    )

    # WHOM they block
    blocked_user = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="blocks_received"
    )

    blocked_org = models.ForeignKey(
        Organization,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="blocks_received"
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "blocks"

        constraints = [
            # Only one blocker type
            models.CheckConstraint(
                condition=(
                    Q(blocker_user__isnull=False, blocker_org__isnull=True) |
                    Q(blocker_user__isnull=True, blocker_org__isnull=False)
                ),
                name="blocker_user_or_org"
            ),
            # Only one blocked target
            models.CheckConstraint(
                condition=(
                    Q(blocked_user__isnull=False, blocked_org__isnull=True) |
                    Q(blocked_user__isnull=True, blocked_org__isnull=False)
                ),
                name="blocked_user_or_org"
            ),
            # One row per identity pair. Partial so the NULL half of each
            # dual-actor column pair never counts toward uniqueness.
            models.UniqueConstraint(
                fields=["blocker_user", "blocked_user"],
                condition=Q(blocker_user__isnull=False, blocked_user__isnull=False),
                name="unique_user_blocks_user"
            ),
            models.UniqueConstraint(
                fields=["blocker_user", "blocked_org"],
                condition=Q(blocker_user__isnull=False, blocked_org__isnull=False),
                name="unique_user_blocks_org"
            ),
            models.UniqueConstraint(
                fields=["blocker_org", "blocked_user"],
                condition=Q(blocker_org__isnull=False, blocked_user__isnull=False),
                name="unique_org_blocks_user"
            ),
            models.UniqueConstraint(
                fields=["blocker_org", "blocked_org"],
                condition=Q(blocker_org__isnull=False, blocked_org__isnull=False),
                name="unique_org_blocks_org"
            ),
            # No identity may block itself
            models.CheckConstraint(
                condition=(
                    ~Q(blocker_user=F("blocked_user")) &
                    ~Q(blocker_org=F("blocked_org"))
                ),
                name="block_not_self"
            )
        ]
        indexes = [
            models.Index(fields=["blocker_user"]),
            models.Index(fields=["blocker_org"]),
            models.Index(fields=["blocked_user"]),
            models.Index(fields=["blocked_org"]),
        ]


    def clean(self):
        # Ensure blocker exists
        if not self.blocker_user and not self.blocker_org:
            raise ValidationError("Blocker must be either a user or an organization.")

        # Ensure blocked target exists
        if not self.blocked_user and not self.blocked_org:
            raise ValidationError("Blocked target must be either a user or an organization.")

        # Prevent user -> same user
        if self.blocker_user and self.blocked_user:
            if self.blocker_user_id == self.blocked_user_id:
                raise ValidationError("Users cannot block themselves.")

        # Prevent org -> same org
        if self.blocker_org and self.blocked_org:
            if self.blocker_org_id == self.blocked_org_id:
                raise ValidationError("Organizations cannot block themselves.")

    def __str__(self):
        blocker = (
            f"User {self.blocker_user_id}"
            if self.blocker_user
            else f"Org {self.blocker_org_id}"
        )

        blocked = (
            f"User {self.blocked_user_id}"
            if self.blocked_user
            else f"Org {self.blocked_org_id}"
        )

        return f"{blocker} -x-> {blocked}"
