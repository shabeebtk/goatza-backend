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
    direct_pair_key = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        unique=True
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
        SHARED_POST = "shared_post", "Shared Post"
        SHARED_RECRUITMENT = "shared_recruitment", "Shared Recruitment"
        # Two profile types rather than one generic "shared_profile": the
        # one-type-one-FK pattern below extends without special-casing, and a
        # single type would need a discriminator column to say which of the two
        # FKs to read.
        SHARED_USER_PROFILE = "shared_user_profile", "Shared User Profile"
        SHARED_ORG_PROFILE = "shared_org_profile", "Shared Organization Profile"

    # Types whose meaning depends on a shared object being attached.
    SHARED_TYPES = (
        Type.SHARED_POST,
        Type.SHARED_RECRUITMENT,
        Type.SHARED_USER_PROFILE,
        Type.SHARED_ORG_PROFILE,
    )

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

    # Caption for shared_* messages, body for text messages.
    content = models.TextField(blank=True)

    # SHARED CONTENT
    # SET_NULL, not CASCADE: deleting a post must not delete the conversation
    # history that referenced it. The message stays, and the serializer renders
    # an "unavailable" preview once the FK is gone.
    shared_post = models.ForeignKey(
        "posts.Post",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+"
    )

    shared_recruitment = models.ForeignKey(
        "recruitments.Recruitment",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+"
    )

    # Forwarded profiles. Same SET_NULL reasoning as the two above: a
    # deactivated account must not take the conversation history with it, so the
    # message survives and the serializer renders an "unavailable" card.
    shared_profile_user = models.ForeignKey(
        "accounts.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+"
    )

    shared_profile_org = models.ForeignKey(
        "organization.Organization",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+"
    )

    # media support
    media_url = models.URLField(blank=True)

    # Media metadata, mirroring posts.PostMedia. Populated by the upload flow
    # (not built yet) — every field is nullable so a metadata-extraction failure
    # can never block a send.
    media_public_id = models.CharField(max_length=255, blank=True)
    media_thumbnail_url = models.URLField(max_length=500, blank=True)
    media_width = models.PositiveIntegerField(null=True, blank=True)
    media_height = models.PositiveIntegerField(null=True, blank=True)
    media_duration_ms = models.PositiveIntegerField(null=True, blank=True)
    media_size_bytes = models.PositiveBigIntegerField(null=True, blank=True)

    # delivery state
    is_deleted = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["conversation", "-created_at"]),
            models.Index(fields=["-created_at"]),
            models.Index(fields=["conversation", "message_type"]),
        ]

        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(sender_user__isnull=False, sender_org__isnull=True) |
                    Q(sender_user__isnull=True, sender_org__isnull=False)
                ),
                name="message_sender_user_or_org"
            ),

            # The obvious constraint here would be the forward one:
            #   message_type=shared_post => shared_post IS NOT NULL
            # We deliberately do NOT enforce that in the DB. It holds at INSERT
            # but is violated later by design: shared_post is SET_NULL, so
            # deleting the post NULLs the column and the constraint would abort
            # the post delete with an IntegrityError. Insert-time integrity is
            # enforced in MessageService instead (_validate_share_payload).
            #
            # What IS safe to enforce in the DB is the reverse direction — a
            # shared FK may only be attached to its matching type, and never
            # more than one at once. NULLing a column can never violate this, so
            # it survives SET_NULL and still stops a mislabeled row being
            # written.
            #
            # Every branch must name EVERY other FK as null, so this grows
            # quadratically with the number of shared types. That is the price
            # of one column per target: it is worth paying while the list is
            # short, and the day it stops being short the answer is a generic
            # (content_type, object_id) pair, not a fifth column.
            models.CheckConstraint(
                condition=(
                    Q(
                        shared_post__isnull=True,
                        shared_recruitment__isnull=True,
                        shared_profile_user__isnull=True,
                        shared_profile_org__isnull=True,
                    )
                    | Q(
                        message_type="shared_post",
                        shared_post__isnull=False,
                        shared_recruitment__isnull=True,
                        shared_profile_user__isnull=True,
                        shared_profile_org__isnull=True,
                    )
                    | Q(
                        message_type="shared_recruitment",
                        shared_recruitment__isnull=False,
                        shared_post__isnull=True,
                        shared_profile_user__isnull=True,
                        shared_profile_org__isnull=True,
                    )
                    | Q(
                        message_type="shared_user_profile",
                        shared_profile_user__isnull=False,
                        shared_post__isnull=True,
                        shared_recruitment__isnull=True,
                        shared_profile_org__isnull=True,
                    )
                    | Q(
                        message_type="shared_org_profile",
                        shared_profile_org__isnull=False,
                        shared_post__isnull=True,
                        shared_recruitment__isnull=True,
                        shared_profile_user__isnull=True,
                    )
                ),
                name="message_shared_object_matches_type"
            ),
        ]