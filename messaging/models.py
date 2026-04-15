from django.db import models
from django.db.models import Q
from shared.models import BaseUUIDModel
from accounts.models import User
from organization.models import Organization


# Create your models here.


class Conversation(BaseUUIDModel):

    class Type(models.TextChoices):
        DIRECT = "direct", "Direct"
        GROUP = "group", "Group"  # future

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        REQUESTED = "requested", "Requested"  # message request
        BLOCKED = "blocked", "Blocked"

    type = models.CharField(
        max_length=20,
        choices=Type.choices,
        default=Type.DIRECT
    )

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.ACTIVE
    )

    # who initiated (important for requests)
    created_by_user = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.CASCADE
    )

    created_by_org = models.ForeignKey(
        Organization,
        null=True,
        blank=True,
        on_delete=models.CASCADE
    )

    # last message optimization (IMPORTANT for chat list)
    last_message = models.ForeignKey(
        "Message",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+"
    )

    last_message_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["last_message_at"]),
        ]



class ConversationParticipant(BaseUUIDModel):

    conversation = models.ForeignKey(
        Conversation,
        on_delete=models.CASCADE,
        related_name="participants"
    )

    # support both user and org
    user = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.CASCADE
    )

    org = models.ForeignKey(
        Organization,
        null=True,
        blank=True,
        on_delete=models.CASCADE
    )

    # unread tracking
    last_read_at = models.DateTimeField(null=True, blank=True)

    # request system
    has_accepted = models.BooleanField(default=False)

    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(user__isnull=False, org__isnull=True) |
                    Q(user__isnull=True, org__isnull=False)
                ),
                name="participant_user_or_org"
            ),
            models.UniqueConstraint(
                fields=["conversation", "user"],
                name="unique_user_participant"
            ),
            models.UniqueConstraint(
                fields=["conversation", "org"],
                name="unique_org_participant"
            ),
        ]

        indexes = [
            models.Index(fields=["conversation"]),
            models.Index(fields=["user"]),
            models.Index(fields=["org"]),
        ]



class Message(BaseUUIDModel):

    class Type(models.TextChoices):
        TEXT = "text", "Text"
        IMAGE = "image", "Image"
        VIDEO = "video", "Video"
        SYSTEM = "system", "System"

    conversation = models.ForeignKey(
        Conversation,
        on_delete=models.CASCADE,
        related_name="messages"
    )

    sender_user = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.CASCADE
    )

    sender_org = models.ForeignKey(
        Organization,
        null=True,
        blank=True,
        on_delete=models.CASCADE
    )

    message_type = models.CharField(
        max_length=20,
        choices=Type.choices,
        default=Type.TEXT
    )

    content = models.TextField(blank=True)

    # media support 
    media_url = models.URLField(blank=True)

    # delivery state
    is_deleted = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["conversation", "-created_at"]),
            models.Index(fields=["-created_at"])
        ]

        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(sender_user__isnull=False, sender_org__isnull=True) |
                    Q(sender_user__isnull=True, sender_org__isnull=False)
                ),
                name="message_sender_user_or_org"
            )
        ]