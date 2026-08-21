from django.db import models
from django.db.models import Q

from shared.models import BaseUUIDModel
from accounts.models import User
from organization.models import Organization
from utils.validations import USERNAME_MAX_LENGTH


class UsernameRegistry(BaseUUIDModel):
    """
    The single namespace users and organizations both draw from.

    Follows the dual-actor pattern used everywhere else in the codebase: two
    optional owner links plus a constraint requiring exactly one. The unique
    constraint on ``username_lower`` is what makes cross-table collisions
    impossible at the database level rather than merely unlikely.

    ``User.username`` and ``Organization.username`` stay as the display columns
    and remain the read path for serializers. This table is the lock.
    """

    username_lower = models.CharField(
        max_length=USERNAME_MAX_LENGTH,
        unique=True,
        db_index=True,
    )

    user = models.OneToOneField(
        User, null=True, blank=True,
        on_delete=models.CASCADE, related_name="username_registration",
    )
    organization = models.OneToOneField(
        Organization, null=True, blank=True,
        on_delete=models.CASCADE, related_name="username_registration",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "username_registry"
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(user__isnull=False, organization__isnull=True)
                    | Q(user__isnull=True, organization__isnull=False)
                ),
                name="username_registry_exactly_one_owner",
            ),
        ]

    def __str__(self):
        return f"@{self.username_lower}"
