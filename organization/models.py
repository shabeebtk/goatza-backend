from django.db import models
from django.conf import settings
from django.db.models import Q
from django.core.exceptions import ValidationError
from shared.models import BaseUUIDModel
from accounts.models import User
from sports.models import Sport

class Organization(BaseUUIDModel):
    class Type(models.TextChoices):
        TEAM = "team", "Team"
        ACADEMY = "academy", "Academy"

    class Level(models.TextChoices):
        AMATEUR = "amateur", "Amateur"
        PROFESSIONAL = "professional", "Professional"
        YOUTH = "youth", "Youth"

    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=255, unique=True)

    type = models.CharField(max_length=20, choices=Type.choices)
    level = models.CharField(max_length=20, choices=Level.choices, blank=True)

    logo = models.URLField(blank=True)
    cover_image = models.URLField(blank=True)

    description = models.TextField(blank=True)
    website = models.URLField(blank=True)

    created_by = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="created_organizations"
    )
    is_verified = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "organizations"
        indexes = [
            models.Index(fields=["slug"]),
            models.Index(fields=["created_by"]),
        ]

    def clean(self):
        if self.slug:
            self.slug = self.slug.lower()

    def __str__(self):
        return self.name
    


class OrganizationMember(BaseUUIDModel):
    class Role(models.TextChoices):
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