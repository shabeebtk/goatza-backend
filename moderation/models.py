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


# Report choices sit at module level, unlike the nested TextChoices used
# elsewhere in the repo: Report.Meta cannot see names declared in Report's own
# class body, and the 12 partial unique constraints below need to name the open
# statuses symbolically rather than repeat raw strings.
class ReportCategory(models.TextChoices):
    SPAM = "spam", "Spam"
    HARASSMENT = "harassment", "Harassment"
    HATE_SPEECH = "hate_speech", "Hate Speech"
    NUDITY_SEXUAL = "nudity_sexual", "Nudity / Sexual Content"
    VIOLENCE = "violence", "Violence"
    SCAM_FRAUD = "scam_fraud", "Scam / Fraud"
    IMPERSONATION_FAKE = "impersonation_fake", "Impersonation / Fake Account"
    MINOR_SAFETY = "minor_safety", "Minor Safety"
    SELF_HARM = "self_harm", "Self Harm"
    OTHER = "other", "Other"


class ReportStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    REVIEWING = "reviewing", "Reviewing"
    ACTION_TAKEN = "action_taken", "Action Taken"
    DISMISSED = "dismissed", "Dismissed"


class ReportAction(models.TextChoices):
    NONE = "none", "None"
    CONTENT_REMOVED = "content_removed", "Content Removed"
    WARNING_SENT = "warning_sent", "Warning Sent"
    ACCOUNT_SUSPENDED = "account_suspended", "Account Suspended"


# A report still awaiting a decision. Dedup only applies while a report is
# open — once it resolves it leaves the partial index, so the same reporter may
# report the same target again if the behaviour continues.
OPEN_REPORT_STATUSES = [ReportStatus.PENDING, ReportStatus.REVIEWING]


class Report(BaseUUIDModel):
    # WHO reported
    reporter_user = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="reports_made"
    )

    reporter_org = models.ForeignKey(
        Organization,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="reports_made"
    )

    # WHAT was reported — exactly one of the six.
    #
    # SET_NULL, not CASCADE: content_snapshot holds the evidence, so a report
    # has to survive its target row disappearing. Soft delete already covers
    # the normal removal path; this covers a later hard delete.
    reported_user = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="reports_received"
    )

    reported_org = models.ForeignKey(
        Organization,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="reports_received"
    )

    reported_post = models.ForeignKey(
        "posts.Post",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="reports_received"
    )

    reported_comment = models.ForeignKey(
        "posts.Comment",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="reports_received"
    )

    reported_message = models.ForeignKey(
        "messaging.Message",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="reports_received"
    )

    reported_recruitment = models.ForeignKey(
        "recruitments.Recruitment",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="reports_received"
    )

    # DETAILS
    category = models.CharField(max_length=30, choices=ReportCategory.choices)
    details = models.TextField(blank=True)

    # Captured at report time — text, media urls, author handle, created_at.
    # An edit after the fact cannot rewrite what was reported.
    content_snapshot = models.JSONField(default=dict, blank=True)

    status = models.CharField(
        max_length=20,
        choices=ReportStatus.choices,
        default=ReportStatus.PENDING,
        db_index=True
    )

    # Severe category, or enough distinct reporters to jump the queue.
    is_priority = models.BooleanField(default=False, db_index=True)

    # RESOLUTION — filled from admin
    reviewed_by = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+"
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    resolution_note = models.TextField(blank=True)
    action_taken = models.CharField(
        max_length=20,
        choices=ReportAction.choices,
        default=ReportAction.NONE
    )

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "reports"

        constraints = [
            # Only one reporter type
            models.CheckConstraint(
                condition=(
                    Q(reporter_user__isnull=False, reporter_org__isnull=True) |
                    Q(reporter_user__isnull=True, reporter_org__isnull=False)
                ),
                name="reporter_user_or_org"
            ),
            # AT MOST one target — six "this column set, the rest null"
            # combinations, plus the all-null row.
            #
            # Not "exactly one", and the seventh Q is the whole reason: the
            # target FKs are SET_NULL so a report OUTLIVES its target, and the
            # moment a reported post is hard-deleted Postgres nulls the only
            # non-null column on every report pointing at it. Under an
            # exactly-one rule that UPDATE is rejected and the delete fails —
            # a report would make its target undeletable, and the
            # "[deleted] — see snapshot" state the admin queue renders could
            # never be reached.
            #
            # Exactly-one at CREATION time is still enforced, in
            # ReportService._validate, which is the layer that knows the
            # difference between "filed with no target" (a bug) and "target
            # was purged later" (the design).
            models.CheckConstraint(
                condition=(
                    Q(reported_user__isnull=False, reported_org__isnull=True, reported_post__isnull=True,
                      reported_comment__isnull=True, reported_message__isnull=True, reported_recruitment__isnull=True) |
                    Q(reported_user__isnull=True, reported_org__isnull=False, reported_post__isnull=True,
                      reported_comment__isnull=True, reported_message__isnull=True, reported_recruitment__isnull=True) |
                    Q(reported_user__isnull=True, reported_org__isnull=True, reported_post__isnull=False,
                      reported_comment__isnull=True, reported_message__isnull=True, reported_recruitment__isnull=True) |
                    Q(reported_user__isnull=True, reported_org__isnull=True, reported_post__isnull=True,
                      reported_comment__isnull=False, reported_message__isnull=True, reported_recruitment__isnull=True) |
                    Q(reported_user__isnull=True, reported_org__isnull=True, reported_post__isnull=True,
                      reported_comment__isnull=True, reported_message__isnull=False, reported_recruitment__isnull=True) |
                    Q(reported_user__isnull=True, reported_org__isnull=True, reported_post__isnull=True,
                      reported_comment__isnull=True, reported_message__isnull=True, reported_recruitment__isnull=False) |
                    # Target purged after the fact. content_snapshot is now the
                    # only record of what was reported.
                    Q(reported_user__isnull=True, reported_org__isnull=True, reported_post__isnull=True,
                      reported_comment__isnull=True, reported_message__isnull=True, reported_recruitment__isnull=True)
                ),
                name="report_at_most_one_target"
            ),

            # DEDUP — one OPEN report per (reporter identity, target).
            # Partial on the identity columns AND the status, so a resolved
            # report drops out of the index and re-reporting stays possible.
            models.UniqueConstraint(
                fields=["reporter_user", "reported_user"],
                condition=Q(reporter_user__isnull=False, reported_user__isnull=False,
                            status__in=OPEN_REPORT_STATUSES),
                name="uniq_open_report_user_user"
            ),
            models.UniqueConstraint(
                fields=["reporter_user", "reported_org"],
                condition=Q(reporter_user__isnull=False, reported_org__isnull=False,
                            status__in=OPEN_REPORT_STATUSES),
                name="uniq_open_report_user_org"
            ),
            models.UniqueConstraint(
                fields=["reporter_user", "reported_post"],
                condition=Q(reporter_user__isnull=False, reported_post__isnull=False,
                            status__in=OPEN_REPORT_STATUSES),
                name="uniq_open_report_user_post"
            ),
            models.UniqueConstraint(
                fields=["reporter_user", "reported_comment"],
                condition=Q(reporter_user__isnull=False, reported_comment__isnull=False,
                            status__in=OPEN_REPORT_STATUSES),
                name="uniq_open_report_user_comment"
            ),
            models.UniqueConstraint(
                fields=["reporter_user", "reported_message"],
                condition=Q(reporter_user__isnull=False, reported_message__isnull=False,
                            status__in=OPEN_REPORT_STATUSES),
                name="uniq_open_report_user_message"
            ),
            models.UniqueConstraint(
                fields=["reporter_user", "reported_recruitment"],
                condition=Q(reporter_user__isnull=False, reported_recruitment__isnull=False,
                            status__in=OPEN_REPORT_STATUSES),
                name="uniq_open_report_user_recruitment"
            ),
            models.UniqueConstraint(
                fields=["reporter_org", "reported_user"],
                condition=Q(reporter_org__isnull=False, reported_user__isnull=False,
                            status__in=OPEN_REPORT_STATUSES),
                name="uniq_open_report_org_user"
            ),
            models.UniqueConstraint(
                fields=["reporter_org", "reported_org"],
                condition=Q(reporter_org__isnull=False, reported_org__isnull=False,
                            status__in=OPEN_REPORT_STATUSES),
                name="uniq_open_report_org_org"
            ),
            models.UniqueConstraint(
                fields=["reporter_org", "reported_post"],
                condition=Q(reporter_org__isnull=False, reported_post__isnull=False,
                            status__in=OPEN_REPORT_STATUSES),
                name="uniq_open_report_org_post"
            ),
            models.UniqueConstraint(
                fields=["reporter_org", "reported_comment"],
                condition=Q(reporter_org__isnull=False, reported_comment__isnull=False,
                            status__in=OPEN_REPORT_STATUSES),
                name="uniq_open_report_org_comment"
            ),
            models.UniqueConstraint(
                fields=["reporter_org", "reported_message"],
                condition=Q(reporter_org__isnull=False, reported_message__isnull=False,
                            status__in=OPEN_REPORT_STATUSES),
                name="uniq_open_report_org_message"
            ),
            models.UniqueConstraint(
                fields=["reporter_org", "reported_recruitment"],
                condition=Q(reporter_org__isnull=False, reported_recruitment__isnull=False,
                            status__in=OPEN_REPORT_STATUSES),
                name="uniq_open_report_org_recruitment"
            ),
        ]

        indexes = [
            # The queue itself: filter by status, newest first.
            models.Index(fields=["status", "-created_at"]),
            models.Index(fields=["reported_user"]),
            models.Index(fields=["reported_org"]),
            models.Index(fields=["reported_post"]),
            models.Index(fields=["reported_comment"]),
            models.Index(fields=["reported_message"]),
            models.Index(fields=["reported_recruitment"]),
        ]

    def __str__(self):
        reporter = (
            f"User {self.reporter_user_id}"
            if self.reporter_user_id
            else f"Org {self.reporter_org_id}"
        )

        target = "nothing"
        for attr, label in (
            ("reported_user_id", "User"),
            ("reported_org_id", "Org"),
            ("reported_post_id", "Post"),
            ("reported_comment_id", "Comment"),
            ("reported_message_id", "Message"),
            ("reported_recruitment_id", "Recruitment"),
        ):
            value = getattr(self, attr)
            if value:
                target = f"{label} {value}"
                break

        return f"{reporter} reported {target} ({self.category})"
