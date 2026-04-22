from django.db import models
from django.conf import settings
from django.db.models import Q
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from shared.models import BaseUUIDModel
from accounts.models import User
from sports.models import Sport


class Organization(BaseUUIDModel):
    class Type(models.TextChoices):
        CLUB = "club", "Club"
        TEAM = "team", "Team"
        ACADEMY = "academy", "Academy"
        SCHOOL = "school", "School / College"

    name = models.CharField(max_length=255)

    # Public unique identifier (used in URL)
    username = models.CharField(
        max_length=50,
        unique=True,
        db_index=True,
        validators=[
            RegexValidator(
                regex=r"^[a-z0-9_.]+$",
                message="Only lowercase letters, numbers, underscore and dot allowed"
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

    logo = models.URLField(blank=True)
    logo_public_id = models.CharField(max_length=255, blank=True)

    cover_image = models.URLField(blank=True)
    cover_image_public_id = models.CharField(max_length=255, blank=True)

    headline = models.CharField(max_length=150, blank=True)
    description = models.TextField(blank=True)
    website = models.URLField(blank=True)

    level = models.CharField(
        max_length=20,
        choices=Level.choices,
        blank=True
    )

    # Denormalized counters (fast reads)
    followers_count = models.PositiveIntegerField(default=0)
    posts_count = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "organization_profiles"

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