from django.db import models
from django.conf import settings
from django.db.models import Q
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from shared.models import BaseUUIDModel, Location
from apps.accounts.models import User
from apps.sports.models import Sport
from utils.timezones import (
    TIMEZONE_MAX_LENGTH,
    default_timezone,
    validate_timezone,
)


class Organization(BaseUUIDModel):
    class Type(models.TextChoices):
        CLUB = "club", "Club"
        TEAM = "team", "Team"
        ACADEMY = "academy", "Academy"
        SCHOOL = "school", "School / College"

    name = models.CharField(max_length=255)

    # Public unique identifier (used in URL).
    #
    # unique=True here is only unique WITHIN this table — the cross-table lock
    # that stops an org taking a handle a user already holds lives in
    # usernames.UsernameRegistry, and every write goes through
    # UsernameService.claim. This column stays the display/read path.
    #
    # The dot is gone: it was the one charset difference between orgs and
    # users, and dropping it is what lets the two share one namespace. The
    # bound is utils.validations.USERNAME_MAX_LENGTH, not this max_length.
    username = models.CharField(
        max_length=50,
        unique=True,
        db_index=True,
        validators=[
            RegexValidator(
                regex=r"^[a-z0-9_]+$",
                message="Only lowercase letters, numbers and underscore allowed"
            )
        ]
    )

    type = models.CharField(max_length=20, choices=Type.choices)

    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        related_name="created_organizations",
        null=True, blank=True
    )

    is_verified = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    # Moderation suspension. Distinct from is_active, which is the org's own
    # lifecycle (deactivated by its owner): this one is imposed, only staff can
    # clear it, and it is what actor resolution refuses to act as. Indexed
    # because every org read path now filters on it.
    is_suspended = models.BooleanField(default=False, db_index=True)

    # THE ORG'S CALENDAR, and the seed for every recruitment it posts.
    #
    # An IANA name ("Asia/Kolkata", "Europe/London"), never an offset: an
    # offset cannot survive a DST boundary, and a trial posted in March for
    # August would be an hour out. Validated against tzdata on save — a typo
    # here silently reschedules every trial this org runs.
    #
    # The default is settings.RECRUITMENT_TIMEZONE, suggested from the org's
    # primary location on create (see timezone_for_country) and editable in
    # org settings. It is a SEED, not a lock: Recruitment.timezone is copied
    # from it at create time and an org posting abroad changes it there,
    # per recruitment, without touching this.
    timezone = models.CharField(
        max_length=TIMEZONE_MAX_LENGTH,
        default=default_timezone,
        validators=[validate_timezone],
    )

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "organizations"
        indexes = [
            models.Index(fields=["username"]),
            models.Index(fields=["type"]),
            models.Index(fields=["created_by"]),
            models.Index(fields=["created_at"]),
        ]

    def clean(self):
        if self.username:
            self.username = self.username.lower().strip()
        validate_timezone(self.timezone)

    def save(self, *args, **kwargs):
        """
        Validate the timezone on the way in, on EVERY write path.

        The field validator above only runs under ``full_clean()``, and most
        of this codebase saves without it. A bad zone cannot be caught by a
        DB constraint either — the tz list is not in SQL — so this is what
        makes "a typo cannot be stored" actually true.

        Skipped when the column is deferred or left out of ``update_fields``:
        reading ``self.timezone`` would otherwise cost a query on a save that
        was never going to touch it.
        """
        fields = kwargs.get("update_fields")
        if (
            (fields is None or "timezone" in fields)
            and "timezone" not in self.get_deferred_fields()
        ):
            validate_timezone(self.timezone)
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.name} (@{self.username})"
    


class OrganizationProfile(BaseUUIDModel):
    class Level(models.TextChoices):
        AMATEUR = "amateur", "Amateur"
        SEMI_PROFESSIONAL = "semi_professional", "Semi Professional"
        PROFESSIONAL = "professional", "Professional"
        YOUTH = "youth", "Youth Development"

    organization = models.OneToOneField(
        Organization,
        on_delete=models.CASCADE,
        related_name="profile"
    )

    logo = models.URLField(max_length=500, blank=True)
    logo_public_id = models.CharField(max_length=255, blank=True)

    cover_image = models.URLField(max_length=500, blank=True)
    cover_image_public_id = models.CharField(max_length=255, blank=True)

    headline = models.CharField(max_length=150, blank=True)
    description = models.TextField(blank=True)
    website = models.URLField(max_length=500, blank=True)

    level = models.CharField(
        max_length=20,
        choices=Level.choices,
        blank=True
    )

    # Denormalized counters (fast reads)
    followers_count = models.PositiveIntegerField(default=0)
    following_count = models.PositiveIntegerField(default=0)
    posts_count = models.PositiveIntegerField(default=0)

    # Governs the LOGGED-OUT web view only (GET /public/organization/<username>).
    # Same semantics as UserProfile.is_public_profile: signed-in actors are
    # unaffected, and a hidden org is still shareable in DMs. Only OWNER/ADMIN
    # members may flip it — enforced in OrganizationPrivacyService.
    is_public_profile = models.BooleanField(
        default=True,
        help_text="Visible to logged-out visitors. Does not affect in-app visibility.",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "organization_profiles"
        indexes = [
            # Explore "popular" mode orders organizations by follower count.
            models.Index(fields=["followers_count"]),
        ]

    def __str__(self):
        return f"{self.organization.name} Profile"



class OrganizationLocation(BaseUUIDModel):
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="locations"
    )

    name = models.CharField(max_length=255, blank=True)  # e.g. "Main Branch"

    address = models.CharField(max_length=500, blank=True)

    # The shared place this branch sits at. Nullable because a branch typed in
    # by hand (or written before the picker existed) has no resolved place, and
    # SET_NULL because losing the place must never delete the branch. The
    # columns below stay as denormalized copies — explore reads them directly,
    # and the refresh job writes coordinates back through this FK.
    location = models.ForeignKey(
        Location,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="organization_locations"
    )

    city = models.CharField(max_length=100, db_index=True)
    state = models.CharField(max_length=100, blank=True)
    country_code = models.CharField(max_length=5, db_index=True)

    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)

    is_primary = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "organization_locations"
        indexes = [
            models.Index(fields=["organization"]),
            models.Index(fields=["city"]),
            models.Index(fields=["country_code"]),
            models.Index(fields=["latitude", "longitude"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["organization"],
                condition=Q(is_primary=True),
                name="unique_primary_location_per_org"
            )
        ]

    def clean(self):
        if self.latitude is not None and not (-90 <= self.latitude <= 90):
            raise ValidationError("Latitude must be between -90 and 90")

        if self.longitude is not None and not (-180 <= self.longitude <= 180):
            raise ValidationError("Longitude must be between -180 and 180")

    def __str__(self):
        return f"{self.organization.name} - {self.city}"


class OrganizationMember(BaseUUIDModel):
    class Role(models.TextChoices):
        OWNER = "owner", "Owner"
        ADMIN = "admin", "Admin"
        COACH = "coach", "Coach"
        STAFF = "staff", "staff"

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="members"
    )
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="organization_memberships"
    )
    role = models.CharField(max_length=20, choices=Role.choices)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "organization_members"
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "user"],
                name="unique_org_user"
            )
        ]
        indexes = [
            models.Index(fields=["organization", "user"]),
        ]

    def __str__(self):
        return f"{self.user_id} - {self.organization.name} ({self.role})"
    


class OrganizationSport(BaseUUIDModel):
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="sports"
    )
    sport = models.ForeignKey(
        Sport,
        on_delete=models.CASCADE,
        related_name="organizations"
    )
    is_primary = models.BooleanField(default=False)

    class Meta:
        db_table = "organization_sports"
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "sport"],
                name="unique_org_sport"
            )
        ]
        indexes = [
            models.Index(fields=["organization", "sport"]),
        ]

    def __str__(self):
        return f"{self.organization.name} - {self.sport.name}"