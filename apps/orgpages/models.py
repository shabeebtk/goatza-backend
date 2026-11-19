"""
An organization's page: the showcase every club, team and academy gets, and
the enquiry form that brings it leads (apps/enquiries).

The page is made of sections. The ones that already existed stay exactly as
they are and read their own data: about (the profile description), sports,
locations, squads, open trials (recruitments) and posts. This app adds the
rest — key facts, contact options, programs & fees, facilities, coaches and
the players the academy developed — and the switch on every section.

THE EMPTY RULE: a section with nothing in it never shows, whatever its switch
says. A switched-off section never shows, whatever is in it.

Everything here cascades with the organization. Every write goes through the
services (apps/orgpages/services), which lock the org's PageSettings row first
and re-check ``page.manage`` after the lock.
"""

from django.db import models
from django.db.models import F, Q

from apps.accounts.models import User
from apps.careers.models import CareerEntry
from apps.organization.models import (
    Organization,
    OrganizationLocation,
    OrganizationMember,
)
from apps.orgpages import constants as C
from apps.sports.models import Sport
from shared.models import BaseUUIDModel


class PageSettings(BaseUUIDModel):
    """
    One row per organization, created lazily (``get_or_create``) the first
    time the page is read or written — there is no backfill.

    ``enquiry_form_enabled`` is decided from ``organization.type`` when the row
    is created: on for an academy, off for a club or a team. It is never
    re-derived afterwards, so an org that changes type keeps its choice.

    Numbers are stored as E.164 (apps/orgpages/phones.py). A number or an email
    is public only while its switch is on; switching it off keeps it stored,
    so switching back on needs nothing re-typed.

    ``sections`` is ``{key: bool}`` for the keys in
    ``constants.SWITCHABLE_SECTIONS``. A missing key means ON.
    """

    class StudentRange(models.TextChoices):
        UNDER_50 = "under_50", "Under 50 students"
        FROM_50_TO_100 = "50_100", "50–100 students"
        FROM_100_TO_200 = "100_200", "100–200 students"
        FROM_200_TO_500 = "200_500", "200–500 students"
        OVER_500 = "500_plus", "500+ students"

    organization = models.OneToOneField(
        Organization,
        on_delete=models.CASCADE,
        related_name="page_settings",
    )

    # ---- contact ----
    enquiry_form_enabled = models.BooleanField(default=False)
    whatsapp_enabled = models.BooleanField(default=False)
    whatsapp_number = models.CharField(max_length=16, blank=True)
    call_enabled = models.BooleanField(default=False)
    call_number = models.CharField(max_length=16, blank=True)
    email_enabled = models.BooleanField(default=False)
    contact_email = models.EmailField(blank=True)

    # ---- key facts ----
    founded_year = models.PositiveSmallIntegerField(null=True, blank=True)
    age_min = models.PositiveSmallIntegerField(null=True, blank=True)
    age_max = models.PositiveSmallIntegerField(null=True, blank=True)
    # ≤ 6 strings of 1–30 chars, trimmed, de-duplicated case-insensitively.
    coaching_languages = models.JSONField(default=list, blank=True)
    # From constants.WEEK_DAYS, stored in week order.
    training_days = models.JSONField(default=list, blank=True)
    student_range = models.CharField(
        max_length=10, choices=StudentRange.choices, blank=True
    )
    instagram_url = models.URLField(max_length=C.SOCIAL_URL_MAX, blank=True)
    youtube_url = models.URLField(max_length=C.SOCIAL_URL_MAX, blank=True)
    facebook_url = models.URLField(max_length=C.SOCIAL_URL_MAX, blank=True)

    # ---- section switches ----
    sections = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "org_page_settings"
        verbose_name = "page settings"
        verbose_name_plural = "page settings"
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(age_min__isnull=True)
                    | Q(age_max__isnull=True)
                    | Q(age_min__lte=F("age_max"))
                ),
                name="org_page_settings_age_order",
            ),
        ]

    @staticmethod
    def default_enquiry_form(organization_type) -> bool:
        """On for an academy, off for a club or a team."""
        return organization_type == Organization.Type.ACADEMY

    def section_enabled(self, key) -> bool:
        if key in C.ALWAYS_SHOWN_SECTIONS:
            return True
        return (self.sections or {}).get(key, True) is not False

    def section_switches(self) -> dict:
        """``{key: bool}`` for every switchable section, missing keys filled in."""
        return {key: self.section_enabled(key) for key in C.SWITCHABLE_SECTIONS}

    def __str__(self):
        return f"Page settings ({self.organization_id})"


class Program(BaseUUIDModel):
    """
    One program on the page ("U12 Football — weekday evenings").

    ``sport`` must be one of the org's OrganizationSport rows at the time it is
    written (the service checks; nothing here can). ``location`` is one of the
    org's branches, or none.

    THE FEE: ``fee_mode=amount`` carries ``fee_amount`` (whole rupees) and
    ``fee_per``; ``fee_mode=contact`` ("Contact for fees") carries neither.
    The DB holds the pairing.
    """

    class Level(models.TextChoices):
        BEGINNER = "beginner", "Beginner"
        INTERMEDIATE = "intermediate", "Intermediate"
        ADVANCED = "advanced", "Advanced"
        ALL_LEVELS = "all_levels", "All levels"

    class FeeMode(models.TextChoices):
        AMOUNT = "amount", "Amount"
        CONTACT = "contact", "Contact for fees"

    class FeePer(models.TextChoices):
        MONTH = "month", "per month"
        TERM = "term", "per term"
        YEAR = "year", "per year"
        SESSION = "session", "per session"

    class Spots(models.TextChoices):
        OPEN = "open", "Spots open"
        FEW = "few", "Few spots left"
        FULL = "full", "Full"

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="page_programs",
    )
    name = models.CharField(max_length=C.NAME_MAX)
    sport = models.ForeignKey(
        Sport,
        on_delete=models.PROTECT,
        related_name="page_programs",
    )
    age_min = models.PositiveSmallIntegerField()
    age_max = models.PositiveSmallIntegerField()
    level = models.CharField(max_length=20, choices=Level.choices)
    # From constants.WEEK_DAYS, at least one, stored in week order.
    days = models.JSONField(default=list)
    start_time = models.TimeField()
    end_time = models.TimeField()
    location = models.ForeignKey(
        OrganizationLocation,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="page_programs",
    )

    fee_mode = models.CharField(max_length=10, choices=FeeMode.choices)
    fee_amount = models.PositiveIntegerField(null=True, blank=True)
    fee_per = models.CharField(
        max_length=10, choices=FeePer.choices, null=True, blank=True
    )
    currency = models.CharField(max_length=3, default="INR")
    free_trial = models.BooleanField(default=False)
    spots = models.CharField(
        max_length=10, choices=Spots.choices, default=Spots.OPEN
    )

    order = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "org_page_programs"
        ordering = ["order", "created_at"]
        indexes = [
            models.Index(fields=["organization", "order"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(end_time__gt=F("start_time")),
                name="org_page_program_ends_after_start",
            ),
            models.CheckConstraint(
                condition=Q(age_min__lte=F("age_max")),
                name="org_page_program_age_order",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        fee_mode="amount",
                        fee_amount__isnull=False,
                        fee_per__isnull=False,
                    )
                    | Q(
                        fee_mode="contact",
                        fee_amount__isnull=True,
                        fee_per__isnull=True,
                    )
                ),
                name="org_page_program_fee_shape",
            ),
        ]

    def __str__(self):
        return f"{self.name} ({self.organization_id})"


class FacilityItem(BaseUUIDModel):
    """
    The facilities catalog — DATA, not code. Seeded by
    ``python manage.py seed_facility_catalog`` (idempotent; it resets the
    fields it seeds). A new facility is a row, never a migration. An item
    switched off (``is_active=False``) leaves the editor's catalog and every
    public page, and stays on the branches that picked it in case it returns.
    """

    class Group(models.TextChoices):
        PLAYING_AREA = "playing_area", "Playing area"
        TRAINING = "training", "Training"
        COMFORT = "comfort", "Comfort"
        CARE_TRAVEL = "care_travel", "Care & travel"

    key = models.SlugField(max_length=40, unique=True)
    label = models.CharField(max_length=60)
    group = models.CharField(max_length=20, choices=Group.choices)
    order = models.PositiveSmallIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "org_page_facility_items"
        ordering = ["group", "order", "label"]

    def __str__(self):
        return self.label


class LocationFacilities(BaseUUIDModel):
    """
    What one branch has: catalog items, plus up to 10 of the club's own words
    ("Swimming pool"). Created on the first PUT for that branch.
    """

    location = models.OneToOneField(
        OrganizationLocation,
        on_delete=models.CASCADE,
        related_name="page_facilities",
    )
    items = models.ManyToManyField(
        FacilityItem,
        blank=True,
        related_name="location_facilities",
    )
    custom_items = models.JSONField(default=list, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "org_page_location_facilities"
        verbose_name_plural = "location facilities"

    def __str__(self):
        return f"Facilities ({self.location_id})"


class FacilityPhoto(BaseUUIDModel):
    """
    A photo of one branch, uploaded as type "org_page". Max 12 per branch.
    Deleting the row deletes the object after commit (the service enqueues
    ``delete_keys``); so does deleting the branch.
    """

    location = models.ForeignKey(
        OrganizationLocation,
        on_delete=models.CASCADE,
        related_name="page_photos",
    )
    photo_url = models.URLField(max_length=C.PHOTO_URL_MAX)
    photo_public_id = models.CharField(max_length=C.PHOTO_ID_MAX)
    order = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "org_page_facility_photos"
        ordering = ["order", "created_at"]
        indexes = [
            models.Index(fields=["location", "order"]),
        ]

    def __str__(self):
        return f"Photo ({self.location_id})"


class Coach(BaseUUIDModel):
    """
    A coach on the page: EITHER a member of the org (``member``) OR somebody
    who isn't on Goatza (``name``, and optionally a photo uploaded as type
    "org_page"). Exactly one of the two — a DB check.

    A member coach's name and photo are their profile's; ``photo_*`` stays
    blank for them (also a DB check).

    ``member`` rows are never deleted — removal flips their status — so a coach
    row can outlive the person's access. THE RELATION-LOOKUP RULE: every read
    keeps only non-member coaches or ``member__status="active"``, in the SAME
    ``filter()`` call. A coach whose member left simply stops showing,
    everywhere, and comes back if they rejoin.
    """

    class Role(models.TextChoices):
        HEAD_COACH = "head_coach", "Head coach"
        ASSISTANT_COACH = "assistant_coach", "Assistant coach"
        GOALKEEPING_COACH = "goalkeeping_coach", "Goalkeeping coach"
        FITNESS_COACH = "fitness_coach", "Fitness coach"
        OTHER = "other", "Coach"

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="page_coaches",
    )
    member = models.ForeignKey(
        OrganizationMember,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="page_coach_roles",
    )
    name = models.CharField(max_length=C.NAME_MAX, blank=True)
    photo_url = models.URLField(max_length=C.PHOTO_URL_MAX, blank=True)
    photo_public_id = models.CharField(max_length=C.PHOTO_ID_MAX, blank=True)

    role = models.CharField(max_length=20, choices=Role.choices)
    # Required when role=other ("Girls coach"), and shown instead of the role.
    title = models.CharField(max_length=C.COACH_TITLE_MAX, blank=True)
    licence = models.CharField(max_length=C.COACH_LICENCE_MAX, blank=True)
    years_coaching = models.PositiveSmallIntegerField(null=True, blank=True)
    is_visible = models.BooleanField(default=True)

    order = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "org_page_coaches"
        ordering = ["order", "created_at"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(member__isnull=False, name="")
                    | (Q(member__isnull=True) & ~Q(name=""))
                ),
                name="org_page_coach_member_or_name",
            ),
            models.CheckConstraint(
                condition=(
                    Q(member__isnull=True)
                    | Q(photo_url="", photo_public_id="")
                ),
                name="org_page_coach_member_has_no_photo",
            ),
            models.CheckConstraint(
                condition=(
                    Q(years_coaching__isnull=True)
                    | Q(years_coaching__lte=C.YEARS_COACHING_MAX)
                ),
                name="org_page_coach_years_range",
            ),
            models.UniqueConstraint(
                fields=["organization", "member"],
                condition=Q(member__isnull=False),
                name="unique_org_page_coach_member",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "order"]),
        ]

    @property
    def is_member(self):
        return self.member_id is not None

    def __str__(self):
        return f"Coach {self.name or self.member_id} ({self.organization_id})"


class FeaturedPlayer(BaseUUIDModel):
    """
    A player the academy developed, on its page.

    OPT-IN ONLY: a player appears only when the academy picks them. Nothing is
    ever added automatically. ``career_entry`` is the VERIFIED entry at this
    org that qualified them; deleting it (or the player deleting their entry)
    removes the row. The public read also re-checks it is still verified and
    still at this org, and applies the squads privacy rules to the player.

    ``highlight`` is the academy's own line ("State U19 squad"). The account
    purge deletes a purged user's rows.
    """

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="featured_players",
    )
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="featured_on_pages",
    )
    career_entry = models.ForeignKey(
        CareerEntry,
        on_delete=models.CASCADE,
        related_name="featured_players",
    )
    highlight = models.CharField(max_length=C.HIGHLIGHT_MAX, blank=True)
    order = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "org_page_featured_players"
        ordering = ["order", "created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "user"],
                name="unique_org_page_featured_player",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "order"]),
        ]

    def __str__(self):
        return f"Featured {self.user_id} ({self.organization_id})"
