from django.db import models
from django.db.models import Q, F
from shared.models import BaseUUIDModel
from accounts.models import User
from organization.models import Organization
from sports.models import Sport, SportPosition
from recruitments.models import RecruitmentApplication


class CareerEntry(BaseUUIDModel):
    """
    One stint in a user's sporting career — a club spell, a loan, a national
    team call-up, an academy year, a coaching or scouting role.

    Entries may overlap on purpose (a loan runs inside a club contract, a
    national team call-up runs alongside everything), so there is deliberately
    no constraint preventing overlapping date ranges.
    """

    class EntryType(models.TextChoices):
        CLUB_TEAM = "club_team", "Club Team"
        NATIONAL_TEAM = "national_team", "National Team"
        LOAN = "loan", "Loan"
        ACADEMY = "academy", "Academy"
        SCHOOL_COLLEGE = "school_college", "School / College"
        OTHER = "other", "Other"

    class SquadLevel(models.TextChoices):
        YOUTH = "youth", "Youth"
        RESERVE = "reserve", "Reserve"
        SENIOR = "senior", "Senior"

    class VerificationStatus(models.TextChoices):
        SELF_REPORTED = "self_reported", "Self Reported"
        PENDING = "pending", "Pending"
        VERIFIED = "verified", "Verified"
        REJECTED = "rejected", "Rejected"

    class Source(models.TextChoices):
        MANUAL = "manual", "Manual"
        RECRUITMENT = "recruitment", "Recruitment"

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="career_entries"
    )

    # Optional link to an org on the platform. Entries for clubs that are not
    # on Goatza (or that later leave it) keep only the denormalized name.
    organization = models.ForeignKey(
        Organization,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="career_entries"
    )
    # Always stored, synced from the linked org on save. Survives org deletion
    # so the career history never loses the club it refers to.
    organization_name = models.CharField(max_length=150)

    sport = models.ForeignKey(
        Sport,
        on_delete=models.PROTECT,
        related_name="career_entries"
    )

    # e.g. "Player", "Captain", "Head Coach", "Youth Scout"
    title = models.CharField(max_length=100)

    entry_type = models.CharField(
        max_length=20,
        choices=EntryType.choices,
        default=EntryType.CLUB_TEAM
    )

    squad_level = models.CharField(
        max_length=20,
        choices=SquadLevel.choices,
        blank=True
    )
    # Free text — "U17", "U19 B", "Colts"
    age_group = models.CharField(max_length=20, blank=True)

    positions = models.ManyToManyField(
        SportPosition,
        blank=True,
        related_name="career_entries"
    )

    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True)
    is_current = models.BooleanField(default=False)

    description = models.TextField(blank=True)

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
        related_name="verified_career_entries"
    )
    verified_at = models.DateTimeField(null=True, blank=True)

    source = models.CharField(
        max_length=20,
        choices=Source.choices,
        default=Source.MANUAL
    )
    # Set when the entry was generated from a selected application. The unique
    # constraint below backs idempotent creation from the recruitment pipeline.
    recruitment_application = models.ForeignKey(
        RecruitmentApplication,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="career_entries"
    )

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "career_entries"
        ordering = ["-start_date", "-created_at"]

        indexes = [
            models.Index(fields=["user", "is_current"]),
            models.Index(fields=["user", "start_date"]),
            models.Index(fields=["organization", "verification_status"]),
        ]

        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(end_date__isnull=True) |
                    Q(end_date__gte=F("start_date"))
                ),
                name="career_entry_valid_date_range"
            ),
            models.CheckConstraint(
                condition=(
                    Q(is_current=False) |
                    Q(end_date__isnull=True)
                ),
                name="career_entry_current_has_no_end_date"
            ),
            models.UniqueConstraint(
                fields=["recruitment_application"],
                condition=Q(recruitment_application__isnull=False),
                name="unique_career_entry_per_application"
            ),
        ]

    def save(self, *args, **kwargs):
        # Keep the denormalized name in step with the linked org. Deleting the
        # org nulls the FK through a queryset update, which never reaches
        # save(), so the last synced name is what survives.
        # Organization.name allows 255 chars, this column 150 — clip rather
        # than let a long club name blow up the insert.
        if self.organization_id:
            self.organization_name = self.organization.name[:150]

        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.title} at {self.organization_name} ({self.user_id})"
