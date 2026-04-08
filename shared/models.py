from django.db import models
from django.db.models import Q
from uuid6 import uuid7

class BaseUUIDModel(models.Model):
    id = models.UUIDField(
        primary_key=True,
        default=uuid7,
        editable=False
    )
    class Meta:
        abstract = True



class Location(BaseUUIDModel):
    class Type(models.TextChoices):
        CITY = "city", "City"
        PLACE = "place", "Place"

    name = models.CharField(max_length=255)
    type = models.CharField(max_length=20, choices=Type.choices)

    city = models.CharField(max_length=100, blank=True)
    state = models.CharField(max_length=100, blank=True)
    country = models.CharField(max_length=100, blank=True)
    country_code = models.CharField(max_length=5, blank=True)

    latitude = models.FloatField()
    longitude = models.FloatField()

    external_id = models.CharField(max_length=255, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["name"]),
            models.Index(fields=["type"]),
            models.Index(fields=["latitude", "longitude"]),
        ]
        constraints = [
            # prevent duplicate same place from external API
            models.UniqueConstraint(
                fields=["external_id"],
                condition=~Q(external_id=""),
                name="unique_external_location"
            ),
            models.UniqueConstraint(
                fields=["latitude", "longitude", "name"],
                name="unique_location_combination"
            )
        ]

    def __str__(self):
        return f"{self.name} ({self.type})"