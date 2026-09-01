"""
"Report a problem" — a user telling us the APP is broken.

This is not abuse reporting. ``moderation.Report`` owns that and stays exactly
as it is: it answers "who did what to whom", which is why it carries six target
FKs, twelve partial unique constraints and a snapshot of the reported content.
A bug report has no target at all. Nobody is accused, nothing is enforced, and
the only thing anyone acts on is a description of what went wrong. Sharing a
table with abuse reports would mean every abuse rule — dedup per target,
priority, enforcement actions — applying to "the upload spinner never stops".

Two consequences worth stating up front:

  * A REPORT MAY BE ANONYMOUS. The screens most likely to break are the ones
    somebody hits before they have a session — login, signup, OTP — so
    ``reported_by`` is nullable and ``contact_email`` exists to reach a
    reporter who has no account to reply through.

  * THE REFERENCE IS THE HANDLE, NOT THE ID. ``reference`` is what a reporter
    quotes in an email; the UUID primary key is never shown. See
    ``support.services.reference`` for why it is generated the way it is, and
    why it must never become a URL.
"""

from django.db import models

from accounts.models import User
from organization.models import Organization
from shared.models import BaseUUIDModel


# Module level, not nested, matching moderation/models.py — these choices are
# named from services, serializers and the admin, and nesting them would make
# every one of those import the model just to spell a status.
class ProblemCategory(models.TextChoices):
    """
    What broke, in the reporter's words rather than ours.

    The labels are the literal options rendered in the sheet: somebody whose
    upload failed should recognise their own situation in the list without
    having to guess which internal bucket it belongs to.
    """

    NOT_WORKING = "not_working", "Something isn't working"
    DISPLAY_ISSUE = "display_issue", "Looks broken or misplaced"
    PERFORMANCE = "performance", "Slow or keeps crashing"
    ACCOUNT_LOGIN = "account_login", "Login or account issue"
    MEDIA_UPLOAD = "media_upload", "Media won't upload or play"
    SUGGESTION = "suggestion", "Suggestion or feedback"
    OTHER = "other", "Something else"


class ProblemStatus(models.TextChoices):
    """
    Where a report sits in OUR queue.

    Internal, and never shown to the reporter: "Won't fix" and "Spam suspect"
    are triage outcomes, not replies.
    """

    NEW = "new", "New"
    TRIAGED = "triaged", "Triaged"
    RESOLVED = "resolved", "Resolved"
    WONT_FIX = "wont_fix", "Won't fix"
    DUPLICATE = "duplicate", "Duplicate"
    SPAM_SUSPECT = "spam_suspect", "Spam suspect"


class ProblemReport(BaseUUIDModel):
    """One "something is broken" report, from a user or from nobody."""

    # The public handle for this report — "any update on GZ-7K4M2P". Written
    # once by the service and not editable afterwards: by the time anyone opens
    # this row the reporter has already quoted the code in an email.
    reference = models.CharField(max_length=12, unique=True, editable=False)

    # WHO reported.
    #
    # Nullable because the report may be anonymous, and SET_NULL rather than
    # CASCADE because a deleted account must not take the bug with it — the
    # crash it describes is still there after the reporter leaves.
    reported_by = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="problem_reports"
    )

    # Which actor the reporter was wearing when it broke. Context, not
    # ownership: the same human files as themselves or as a club, and "it only
    # breaks on the club account" is exactly the detail that turns an
    # unreproducible report into a fixable one.
    acting_org = models.ForeignKey(
        Organization,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="problem_reports"
    )

    # WHAT broke.
    category = models.CharField(max_length=30, choices=ProblemCategory.choices)
    description = models.TextField()

    # Cloudinary URLs of whatever the reporter attached. A list of strings, not
    # an FK to a media table: these are throwaway evidence belonging to one
    # report, never part of anybody's library, and nothing else in the product
    # ever queries them.
    screenshots = models.JSONField(default=list, blank=True)

    # How to reach an anonymous reporter. Blank on an authenticated report,
    # where the account already carries an address.
    contact_email = models.EmailField(blank=True)

    # Whatever the client knew at the moment it broke — route, viewport, app
    # version, platform, connection. Free-form on purpose: the field that
    # cracks a report is usually the one we had not thought to ask for, and a
    # schema here would mean a migration every time the client learns to send
    # something new.
    client_context = models.JSONField(default=dict, blank=True)

    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=500, blank=True)

    # OUR SIDE — everything below is written by us, never by the reporter.
    status = models.CharField(
        max_length=20,
        choices=ProblemStatus.choices,
        default=ProblemStatus.NEW,
        db_index=True
    )
    internal_note = models.TextField(blank=True)
    resolved_by = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+"
    )
    resolved_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    # NO CHECK CONSTRAINTS ON THIS MODEL, deliberately.
    #
    # The tempting one is "reported_by is null implies contact_email is set" —
    # an anonymous report nobody can reply to is close to useless. It is still
    # wrong at the database layer: ``reported_by`` is SET_NULL, so hard
    # deleting a user turns an old AUTHENTICATED report into an anonymous one,
    # and that report correctly has a blank contact_email because the account
    # carried the address. The constraint would reject that UPDATE, and a
    # report would end up blocking its own reporter's account deletion.
    #
    # Same failure mode ``moderation.Report.report_at_most_one_target``
    # documents, and the same resolution: enforce the rule where the
    # distinction is visible. "Anonymous reports need an email" is a rule about
    # SUBMISSION, and the public serializer is the only layer that can tell
    # "filed with no account" apart from "the account was deleted a year later".

    class Meta:
        db_table = "problem_reports"
        ordering = ["-created_at"]

        indexes = [
            # The queue itself: filter by status, newest first.
            models.Index(fields=["status", "-created_at"]),
            # "What else has this person reported" — the second question asked
            # about any report that turns out to be part of a pattern.
            models.Index(fields=["reported_by"]),
            # Lookup by the code a reporter quoted. Redundant with the unique
            # constraint on PostgreSQL, kept because support correspondence is
            # THE access path for this column and it should not rely on
            # remembering that unique implies indexed.
            models.Index(fields=["reference"]),
        ]

    def __str__(self):
        reporter = (
            f"@{self.reported_by.username}"
            if self.reported_by_id
            else (self.contact_email or "anonymous")
        )

        return f"{self.reference} · {self.category} · {reporter}"
