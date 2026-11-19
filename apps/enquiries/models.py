"""
Enquiries: the lead form on an organization's page (apps/orgpages), and the
inbox the org works them from.

ANYONE CAN SEND ONE — no account needed. A signed-in sender is recorded
(``submitted_by``) but never required. What is stored is only what the form
asked for, and a minor's own contact is never stored: an under-18 asking for
themselves is refused (``guardian_needed``) and the client switches the form
to "My child".

Spam never reaches this table: a filled honeypot is answered like a success
and nothing is written, and a refused submission writes nothing either.

RETENTION: an enquiry closed (joined / not interested) more than 365 days ago
is deleted by ``purge_closed_enquiries``. The account purge deletes the
enquiries a purged user sent. An org's enquiries cascade with the org.
"""

from django.db import models

from apps.accounts.models import User
from apps.organization.models import (
    Organization,
    OrganizationLocation,
    OrganizationMember,
)
from shared.models import BaseUUIDModel

PLAYER_NAME_MAX = 80
MESSAGE_MAX = 500
NOTES_MAX = 2000


class Enquiry(BaseUUIDModel):
    """
    One enquiry. ``program_name`` / ``location_name`` are snapshots: the
    program or branch can be renamed or deleted (SET_NULL) and the enquiry
    still says what was asked about.

    ``notes`` is the team's own and never leaves the inbox. ``alert_emailed_at``
    is when an email first told the team about this enquiry (the immediate
    email, or a digest); null while it waits (see alert_service).
    """

    class Status(models.TextChoices):
        NEW = "new", "New"
        CONTACTED = "contacted", "Contacted"
        JOINED = "joined", "Joined"
        NOT_INTERESTED = "not_interested", "Not interested"

    CLOSED_STATUSES = (Status.JOINED, Status.NOT_INTERESTED)

    class ForWhom(models.TextChoices):
        CHILD = "child", "My child"
        SELF = "self", "Myself"

    class BestTime(models.TextChoices):
        MORNING = "morning", "Morning"
        AFTERNOON = "afternoon", "Afternoon"
        EVENING = "evening", "Evening"

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="enquiries",
    )

    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.NEW
    )
    status_changed_at = models.DateTimeField(null=True, blank=True)
    status_changed_by = models.ForeignKey(
        OrganizationMember,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    # Set when the status becomes joined or not_interested, cleared when it
    # goes back to new or contacted. What retention counts from.
    closed_at = models.DateTimeField(null=True, blank=True, db_index=True)

    for_whom = models.CharField(max_length=10, choices=ForWhom.choices)
    player_name = models.CharField(max_length=PLAYER_NAME_MAX)
    player_age = models.PositiveSmallIntegerField()
    # Required for a child, blank for an adult asking for themselves.
    guardian_name = models.CharField(max_length=PLAYER_NAME_MAX, blank=True)

    # E.164, via apps/orgpages/phones.py.
    phone = models.CharField(max_length=16)
    phone_is_whatsapp = models.BooleanField(default=False)
    email = models.EmailField(blank=True)

    program = models.ForeignKey(
        "orgpages.Program",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="enquiries",
    )
    program_name = models.CharField(max_length=80, blank=True)
    location = models.ForeignKey(
        OrganizationLocation,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="enquiries",
    )
    location_name = models.CharField(max_length=255, blank=True)

    best_time = models.CharField(
        max_length=10, choices=BestTime.choices, blank=True
    )
    message = models.TextField(blank=True)

    # The team's only. Never on anything the sender can see.
    notes = models.TextField(blank=True)
    notes_updated_by = models.ForeignKey(
        OrganizationMember,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    notes_updated_at = models.DateTimeField(null=True, blank=True)

    # Set when the sender was signed in; never required.
    submitted_by = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="sent_enquiries",
    )

    alert_emailed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "enquiries"
        ordering = ["-created_at", "-id"]
        verbose_name_plural = "enquiries"
        indexes = [
            models.Index(fields=["organization", "status", "created_at"]),
            models.Index(fields=["phone", "created_at"]),
        ]

    def __str__(self):
        return f"Enquiry {self.id} ({self.organization_id}, {self.status})"


class EnquiryAlertState(BaseUUIDModel):
    """
    How many enquiry emails an org has had, for the window and the daily cap
    (alert_service). One row per org, created on its first enquiry, and the
    row whose lock every submission and every digest takes first.

    ``emails_day`` is a date in Asia/Kolkata; ``emails_today`` counts the
    emails sent on that day and starts again on the next.
    """

    organization = models.OneToOneField(
        Organization,
        on_delete=models.CASCADE,
        related_name="enquiry_alert_state",
    )
    last_email_at = models.DateTimeField(null=True, blank=True)
    emails_day = models.DateField(null=True, blank=True)
    emails_today = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "enquiry_alert_states"

    def __str__(self):
        return f"Enquiry alerts ({self.organization_id})"
