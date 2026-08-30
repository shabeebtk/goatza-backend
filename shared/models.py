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
    """
    One real-world place, shared by every model that tags a location.

    ``external_id`` is the provider's own id — a Google ``place_id`` when
    ``provider`` is ``google`` — and it, not the coordinates, is the identity of
    the row. Coordinates are a CACHE of what the provider last said: they are
    nullable, stamped with ``coords_fetched_at``, refreshed through the place id
    and nulled once they go stale and nothing active still points here (see
    docs/PLACES_MIGRATION.md section 6). A row therefore keeps its label and its
    place id forever while its point comes and goes.
    """

    class Type(models.TextChoices):
        CITY = "city", "City"
        PLACE = "place", "Place"

    class Provider(models.TextChoices):
        GOOGLE = "google", "Google"
        MANUAL = "manual", "Manual"

    name = models.CharField(max_length=255)
    type = models.CharField(max_length=20, choices=Type.choices)

    # Who resolved this place. Namespaces external_id: the same string can mean
    # different places to different providers, so uniqueness is per provider.
    provider = models.CharField(
        max_length=20,
        choices=Provider.choices,
        default=Provider.GOOGLE
    )

    city = models.CharField(max_length=100, blank=True)
    state = models.CharField(max_length=100, blank=True)
    country = models.CharField(max_length=100, blank=True)
    country_code = models.CharField(max_length=5, blank=True)

    # Nullable: coordinates expire. NULL means "we no longer know where this is"
    # — haversine treats it as an unknown distance and explore's bounding box
    # drops it, which is exactly the intended behaviour for an expired row.
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)

    external_id = models.CharField(max_length=255, blank=True)

    # When latitude/longitude last came from the provider. NULL = never fetched.
    coords_fetched_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["name"]),
            models.Index(fields=["type"]),
            models.Index(fields=["latitude", "longitude"]),
            # The refresh/expire job scans by staleness.
            models.Index(fields=["coords_fetched_at"]),
        ]
        constraints = [
            # One row per (provider, external_id) — prevents a duplicate of the
            # same place from the same provider. There is deliberately NO
            # uniqueness on (latitude, longitude, name) any more: coordinates
            # are nullable now, so that constraint would collapse every
            # coordinate-less row of the same name into one.
            models.UniqueConstraint(
                fields=["provider", "external_id"],
                condition=~Q(external_id=""),
                name="unique_provider_external_location"
            ),
        ]

    def __str__(self):
        return f"{self.name} ({self.type})"