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
        RECRUITMENT_APPLICATION = "recruitment_application", "Recruitment Application"
        RECRUITMENT_APPLICATION_STATUS = "recruitment_application_status", "Recruitment Application Status"
        MESSAGE = "message", "Message"
        CAREER_VERIFICATION_REQUEST = "career_verification_request", "Career Verification Request"
        CAREER_VERIFIED = "career_verified", "Career Verified"
        CAREER_REJECTED = "career_rejected", "Career Rejected"
        CAREER_ADD_PROMPT = "career_add_prompt", "Career Add Prompt"
        # future:
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
    type = models.CharField(max_length=30, choices=Type.choices)

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

    recruitment = models.ForeignKey(
        "recruitments.Recruitment",
        null=True,
        blank=True,
        on_delete=models.CASCADE
    )
    # Career entries are HARD deleted (structured profile data, not content), so
    # the FK is what keeps a removed entry from leaving orphan notifications
    # deep-linking to a dead id.
    career_entry = models.ForeignKey(
        "careers.CareerEntry",
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