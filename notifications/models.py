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
        MENTION = "mention", "Mention"
        RECRUITMENT_APPLICATION = "recruitment_application", "Recruitment Application"
        RECRUITMENT_APPLICATION_STATUS = "recruitment_application_status", "Recruitment Application Status"
        MESSAGE = "message", "Message"
        CAREER_VERIFICATION_REQUEST = "career_verification_request", "Career Verification Request"
        CAREER_VERIFIED = "career_verified", "Career Verified"
        CAREER_REJECTED = "career_rejected", "Career Rejected"
        CAREER_ADD_PROMPT = "career_add_prompt", "Career Add Prompt"
        ACHIEVEMENT_VERIFICATION_REQUEST = "achievement_verification_request", "Achievement Verification Request"
        ACHIEVEMENT_VERIFIED = "achievement_verified", "Achievement Verified"
        ACHIEVEMENT_REJECTED = "achievement_rejected", "Achievement Rejected"
        # PLATFORM notification: no actor_user / actor_org. Goatza itself is
        # speaking, and naming the moderator who decided would hand the warned
        # account someone to retaliate against.
        MODERATION_WARNING = "moderation_warning", "Moderation Warning"
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
    #
    # 40, not 30: "achievement_verification_request" is 32 and no longer fits
    # the width "recruitment_application_status" (30) used to set.
    type = models.CharField(max_length=40, choices=Type.choices)

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
    # Same story for achievements: hard deleted structured profile data, so the
    # CASCADE is what stops a removed award leaving orphan notifications deep-
    # linking to a dead id.
    achievement = models.ForeignKey(
        "achievements.Achievement",
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

            # AT MOST one actor.
            #
            # The all-null branch is for PLATFORM notifications — a moderation
            # warning is sent by Goatza, not by a person or a club, and there
            # is no identity to put in either column. Stamping the acting
            # moderator there instead would leak who made the call to the
            # account being warned.
            models.CheckConstraint(
                condition=(
                    Q(actor_user__isnull=False, actor_org__isnull=True) |
                    Q(actor_user__isnull=True, actor_org__isnull=False) |
                    Q(actor_user__isnull=True, actor_org__isnull=True)
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