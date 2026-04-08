from django.db import models
from accounts.models import User
from django.core.exceptions import ValidationError
from django.db.models import Q, F
from shared.models import BaseUUIDModel


class Sport(BaseUUIDModel):
    name = models.CharField(max_length=100, unique=True)

    icon_name = models.CharField(max_length=100, blank=True, help_text="iconify library icon name")
    icon_url = models.URLField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "sports"
        ordering = ["name"]
        indexes = [
            models.Index(fields=["name"]),
        ]

    def clean(self):
        if not self.icon_name and not self.icon_url:
            raise ValidationError("Either icon_name or icon_url must be provided.")

    def __str__(self):
        return self.name
    

class SportAttribute(BaseUUIDModel):
    class DataType(models.TextChoices):
        SELECT = "select", "Select"
        MULTI_SELECT = "multi_select", "Multi Select"
        TEXT = "text", "Text"
        NUMBER = "number", "Number"
        BOOLEAN = "boolean", "Boolean"

    sport = models.ForeignKey(
        Sport,
        on_delete=models.CASCADE,
        related_name="attributes"
    )
    name = models.CharField(max_length=100)

    data_type = models.CharField(max_length=20, choices=DataType.choices)

    is_required = models.BooleanField(default=False)
    display_order = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "sport_attributes"
        ordering = ["display_order"]
        constraints = [
            models.UniqueConstraint(fields=["sport", "name"], name="unique_sport_attribute"),
        ]
        indexes = [
            models.Index(fields=["sport", "display_order"]),
        ]

    def __str__(self):
        return f"{self.name} ({self.sport.name})"
    

class SportAttributeOption(BaseUUIDModel):
    attribute = models.ForeignKey(
        SportAttribute,
        on_delete=models.CASCADE,
        related_name="options"
    )
    value = models.CharField(max_length=100)

    class Meta:
        db_table = "sport_attribute_options"
        constraints = [
            models.UniqueConstraint(fields=["attribute", "value"], name="unique_attribute_option"),
        ]

    def __str__(self):
        return f"{self.value} ({self.attribute.name})"
    
    
class UserSport(BaseUUIDModel):
    class ExperienceLevel(models.TextChoices):
        BEGINNER = "beginner", "Beginner"
        INTERMEDIATE = "intermediate", "Intermediate"
        ADVANCED = "advanced", "Advanced"
        PROFESSIONAL = "professional", "Professional"

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="sports"
    )
    sport = models.ForeignKey(
        Sport,
        on_delete=models.CASCADE,
        related_name="users"
    )

    is_primary = models.BooleanField(default=False)
    experience_level = models.CharField(
        max_length=20,
        choices=ExperienceLevel.choices,
        blank=True,
        null=True
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "user_sports"
        constraints = [
            models.UniqueConstraint(fields=["user", "sport"], name="unique_user_sport"),
            models.UniqueConstraint(
                fields=["user"],
                condition=Q(is_primary=True),
                name="unique_primary_sport_per_user"
            ),
        ]
        indexes = [
            models.Index(fields=["user", "sport"]),
        ]

    def __str__(self):
        return f"{self.user_id} - {self.sport.name}"
    
class SportPosition(BaseUUIDModel):
    sport = models.ForeignKey(
        Sport,
        on_delete=models.CASCADE,
        related_name="positions"
    )
    name = models.CharField(max_length=100)

    class Meta:
        db_table = "sport_positions"
        constraints = [
            models.UniqueConstraint(fields=["sport", "name"], name="unique_position_per_sport"),
        ]

    def __str__(self):
        return f"{self.name} ({self.sport.name})"
    

class UserSportPosition(BaseUUIDModel):
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="positions"
    )
    sport = models.ForeignKey(Sport, on_delete=models.CASCADE)
    position = models.ForeignKey(SportPosition, on_delete=models.CASCADE)

    is_primary = models.BooleanField(default=False)

    class Meta:
        db_table = "user_positions"
        constraints = [
            models.UniqueConstraint(fields=["user", "position"], name="unique_user_position"),
            models.UniqueConstraint(
                fields=["user", "sport"],
                condition=Q(is_primary=True),
                name="unique_primary_position_per_sport"
            ),
        ]
        indexes = [
            models.Index(fields=["user", "sport"]),
        ]

    def clean(self):
        if self.position.sport_id != self.sport_id:
            raise ValidationError("Position does not belong to selected sport.")

    def __str__(self):
        return f"{self.user_id} - {self.position.name}"
    

class UserAttributeValue(BaseUUIDModel):
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="attributes"
    )
    sport = models.ForeignKey(Sport, on_delete=models.CASCADE)
    attribute = models.ForeignKey(SportAttribute, on_delete=models.CASCADE)

    option = models.ForeignKey(
        SportAttributeOption,
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )
    value_text = models.CharField(max_length=255, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "user_attribute_values"
        indexes = [
            models.Index(fields=["user", "sport"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(option__isnull=False) | Q(value_text__isnull=False),
                name="attribute_value_required"
            )
        ]

    def clean(self):
        # Ensure attribute belongs to sport
        if self.attribute.sport_id != self.sport_id:
            raise ValidationError("Attribute does not belong to selected sport.")

        # Validate based on type
        if self.attribute.data_type in ["select"]:
            if not self.option:
                raise ValidationError("Option required for select type.")

        if self.attribute.data_type in ["text", "number"]:
            if not self.value_text:
                raise ValidationError("Value required.")

    def __str__(self):
        return f"{self.user_id} - {self.attribute.name}"