from django.db import models
from django.db.models import Q
from shared.models import BaseUUIDModel
from accounts.models import User
from organization.models import Organization
from sports.models import Sport
from careers.models import CareerEntry


class Achievement(BaseUUIDModel):
    """
    One award a user showcases on their profile — a trophy lifted with a team,
    an individual prize, a record broken, a milestone reached, a coaching or
    scouting certification earned.

    Where a CareerEntry is a *stint* — a span of time somewhere, which is why it
    carries a start and an end — an achievement is a *moment*: it happened on
    one day, so there is a single ``achieved_date`` and no range. Several may
    fall on the same date (a cup final can hand out a trophy and a man-of-the-
    match award), so nothing here is unique per user or per date.

    An achievement may name the stint it happened in through ``career_entry``,
    but never has to: awards won outside any tracked stint, or before the user
    started keeping a career history, are ordinary.
    """

    class AchievementType(models.TextChoices):
        TEAM_TROPHY = "team_trophy", "Team Trophy"
        INDIVIDUAL_AWARD = "individual_award", "Individual Award"
        RECORD = "record", "Record"
        MILESTONE = "milestone", "Milestone"
        CERTIFICATION = "certification", "Certification"
        OTHER = "other", "Other"

    class Level(models.TextChoices):
        SCHOOL = "school", "School"
        DISTRICT = "district", "District"
        STATE = "state", "State"
        NATIONAL = "national", "National"
        INTERNATIONAL = "international", "International"
        CLUB_LOCAL = "club_local", "Club / Local"

    class VerificationStatus(models.TextChoices):
        SELF_REPORTED = "self_reported", "Self Reported"
        PENDING = "pending", "Pending"
        VERIFIED = "verified", "Verified"
        REJECTED = "rejected", "Rejected"

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="achievements"
    )

    title = models.CharField(max_length=150)

    achievement_type = models.CharField(
        max_length=20,
        choices=AchievementType.choices,
        default=AchievementType.INDIVIDUAL_AWARD
    )

    sport = models.ForeignKey(
        Sport,
        on_delete=models.PROTECT,
        related_name="achievements"
    )

    description = models.TextField(blank=True)

    # e.g. "Kerala Premier League 2024", "State U19 Championship"
    event_name = models.CharField(max_length=150, blank=True)

    level = models.CharField(
        max_length=20,
        choices=Level.choices,
        blank=True
    )

    # Optional link to the issuing org on the platform. Awards handed out by a
    # federation that is not on Goatza (or that later leaves it) keep only the
    # denormalized name.
    awarded_by = models.ForeignKey(
        Organization,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="issued_achievements"
    )
    # Synced from the linked org on save, so the award never loses the body that
    # issued it when that org is deleted. Unlike CareerEntry.organization_name
    # this column also stands alone as free text — an off-platform federation —
    # and may be empty outright, because plenty of achievements have no issuer.
    awarded_by_name = models.CharField(max_length=150, blank=True)

    # The stint this was won during. Optional: an award can predate the user's
    # career history, or fall outside every stint they chose to record.
    career_entry = models.ForeignKey(
        CareerEntry,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="achievements"
    )

    achieved_date = models.DateField()

    # Single proof/showcase image — the certificate scan or the trophy photo.
    #
    # 500, not URLField's default 200, for the reason Highlight.file_url carries:
    # a Cloudinary public_id is an actor-scoped path of UUIDs (~120 chars) and
    # the secure_url wraps it in the delivery prefix, which lands close enough to
    # 200 that the default used to fail the insert in production only.
    image = models.URLField(max_length=500, blank=True)
    image_public_id = models.CharField(max_length=255, blank=True)

    # News article, federation results page, anything corroborating the claim.
    reference_link = models.URLField(blank=True)

    is_pinned = models.BooleanField(default=False)

    verification_status = models.CharField(
        max_length=20,
        choices=VerificationStatus.choices,
        default=VerificationStatus.SELF_REPORTED
    )
    verified_by = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="verified_achievements"
    )
    verified_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "achievements"
        ordering = ["-is_pinned", "-achieved_date", "-created_at"]

        indexes = [
            models.Index(fields=["user", "is_pinned"]),
            models.Index(fields=["user", "achieved_date"]),
            models.Index(fields=["awarded_by", "verification_status"]),
        ]

        constraints = [
            # A verification state only means something when there is somebody
            # who can hold it: pending/verified/rejected all name a decision an
            # org either owes or has made. With no linked org there is nobody to
            # ask, so the row can only ever be self_reported.
            #
            # Deleting the org nulls awarded_by through a queryset update, which
            # would strand a decided row — the service resets the status in the
            # same breath (Stage 2), and this constraint is what makes forgetting
            # to a hard error rather than a quiet inconsistency.
            models.CheckConstraint(
                condition=(
                    Q(awarded_by__isnull=False) |
                    Q(verification_status="self_reported")
                ),
                name="achievement_verification_requires_issuer"
            ),
            # No constraint for "achieved_date is not in the future": a
            # CheckConstraint cannot reference now(), so the service owns it.
        ]

    def save(self, *args, **kwargs):
        # Keep the denormalized issuer in step with the linked org. Deleting the
        # org nulls the FK through a queryset update, which never reaches
        # save(), so the last synced name is what survives.
        # Organization.name allows 255 chars, this column 150 — clip rather
        # than let a long federation name blow up the insert.
        if self.awarded_by_id:
            self.awarded_by_name = self.awarded_by.name[:150]

        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.title} ({self.user_id})"
