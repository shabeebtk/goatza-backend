from django.db import models
from shared.models import BaseUUIDModel
from accounts.models import User
from organization.models import Organization
from django.db.models import Q

# Create your models here.

class Notification(BaseUUIDModel):

    class Type(models.TextChoices):
        FOLLOW = "follow", "Follow"
        FOLLOW_BACK = "follow_back", "Follow Back"
        LIKE = "like", "Like"
        COMMENT = "comment", "Comment"
        # future:
        # MESSAGE = "message"
        # TRIAL = "trial"
        

    # WHO RECEIVES
    recipient_user = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="notifications"
    )

    recipient_org = models.ForeignKey(
        Organization,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="notifications"
    )

    # WHO TRIGGERED
    actor_user = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="triggered_notifications"
    )

    actor_org = models.ForeignKey(
        Organization,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="triggered_notifications"
    )

    # TYPE
    type = models.CharField(max_length=20, choices=Type.choices)

    # TARGET OBJECTS
    post = models.ForeignKey(
        "posts.Post",
        null=True,
        blank=True,
        on_delete=models.CASCADE
    )

    comment = models.ForeignKey(
        "posts.Comment",
        null=True,
        blank=True,
        on_delete=models.CASCADE
    )
    data = models.JSONField(default=dict, blank=True)

    # STATE
    is_read = models.BooleanField(default=False)

    # soft delete 
    is_deleted = models.BooleanField(default=False)

    # GROUPING KEY 
    group_key = models.CharField(max_length=255, blank=True, db_index=True)

    # DEDUP KEY 
    dedup_key = models.CharField(max_length=255, blank=True, null=True, unique=True)

    # batching support 
    is_batched = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "notifications"

        indexes = [
            models.Index(fields=["recipient_user", "-created_at"]),
            models.Index(fields=["recipient_org", "-created_at"]),
            models.Index(fields=["is_read"]),
            models.Index(fields=["type"]),
        ]

        constraints = [
            # recipient must be one
            models.CheckConstraint(
                condition=(
                    Q(recipient_user__isnull=False, recipient_org__isnull=True) |
                    Q(recipient_user__isnull=True, recipient_org__isnull=False)
                ),
                name="notification_recipient_user_or_org"
            ),

            # actor must be one
            models.CheckConstraint(
                condition=(
                    Q(actor_user__isnull=False, actor_org__isnull=True) |
                    Q(actor_user__isnull=True, actor_org__isnull=False)
                ),
                name="notification_actor_user_or_org"
            ),
        ]

    def __str__(self):
        return f"{self.type} -> {self.id}"
    


class UserFCMToken(BaseUUIDModel):
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="fcm_tokens"
    )

    token = models.CharField(max_length=255, unique=True)

    device_type = models.CharField(
        max_length=20,
        blank=True,
        null=True
    )  # web / android / ios

    device_name = models.CharField(
        max_length=500,
        blank=True,
        null=True
    )  # Chrome / iPhone / etc

    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user}  --> {self.token}"