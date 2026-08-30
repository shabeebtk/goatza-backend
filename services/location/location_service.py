"""
The one write path for ``shared.Location``.

A Location is identified by ``(provider, external_id)`` — a Google ``place_id``
under the ``google`` provider — and NOT by its coordinates. Coordinates are a
cache of what the provider last said, so they are nullable, stamped with
``coords_fetched_at`` and refreshed opportunistically:

  * whenever a client writes a place it just picked, the coordinates in that
    payload are free and fresh (the browser paid for the Details call in the
    same session), so an existing row takes them and re-stamps its timestamp;
  * everything that already copied those coordinates is caught up through
    ``propagate_coords``, because the denormalized columns — not the FK — are
    what distance queries actually read.

See docs/PLACES_MIGRATION.md sections 5.5 and 6.
"""

from django.db import IntegrityError, transaction
from django.utils import timezone

from shared.models import Location


class LocationService:

    @staticmethod
    def normalize_data(data: dict) -> dict:
        """Normalize incoming location data"""
        provider = (data.get("provider") or Location.Provider.GOOGLE).strip()
        if provider not in Location.Provider.values:
            provider = Location.Provider.GOOGLE

        return {
            "provider": provider,
            "name": data.get("name", "").strip(),
            "type": data.get("type", Location.Type.PLACE),
            "city": data.get("city", "").strip(),
            "state": data.get("state", "").strip(),
            "country": data.get("country", "").strip(),
            "country_code": data.get("country_code", "").upper(),
            "latitude": data.get("latitude"),
            "longitude": data.get("longitude"),
            "external_id": data.get("external_id", "").strip(),
        }

    @staticmethod
    def validate(lat, lng):
        if lat is None or lng is None:
            raise ValueError("Latitude and Longitude are required")

        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            raise ValueError("Invalid latitude/longitude values")

    @staticmethod
    def has_coords(lat, lng) -> bool:
        """``validate`` as a question rather than an exception."""
        try:
            LocationService.validate(lat, lng)
        except (TypeError, ValueError):
            return False
        return True

    @staticmethod
    @transaction.atomic
    def get_or_create_location(data: dict) -> Location:
        data = LocationService.normalize_data(data)

        provider = data["provider"]
        external_id = data["external_id"]
        lat = data["latitude"]
        lng = data["longitude"]

        coords_supplied = LocationService.has_coords(lat, lng)

        # 1. The place id IS the identity. Lookup is per provider: the same
        #    string means different things to Google and to a manual entry.
        if external_id:
            location = (
                Location.objects
                .filter(provider=provider, external_id=external_id)
                .first()
            )

            if location:
                # 2. The client just fetched these coordinates in-session, so
                #    taking them costs nothing and buys the row another full
                #    refresh window.
                if coords_supplied:
                    location.latitude = lat
                    location.longitude = lng
                    location.coords_fetched_at = timezone.now()
                    location.save(
                        update_fields=[
                            "latitude",
                            "longitude",
                            "coords_fetched_at",
                        ]
                    )
                    LocationService.propagate_coords(location)

                return location

        # 3. Create. A brand new row still needs a point — a place with neither
        #    a known id on file nor coordinates is not something anything can
        #    use, and this is the same ValueError callers already handle.
        LocationService.validate(lat, lng)

        try:
            return Location.objects.create(
                **data,
                coords_fetched_at=timezone.now()
            )
        except IntegrityError:
            # Lost the race on the (provider, external_id) unique index — the
            # row the winner wrote is the answer.
            if not external_id:
                raise

            return (
                Location.objects
                .filter(provider=provider, external_id=external_id)
                .first()
            )

    @staticmethod
    def propagate_coords(location: Location) -> dict:
        """
        Push ``location``'s coordinates onto every row that copied them.

        Distance is computed off the DENORMALIZED columns, never off the FK, so
        a refreshed (or expired) Location changes nothing until this runs. NULLs
        propagate exactly like values do — that is how an expired place leaves
        nearby results.

        Returns the per-model row counts, for the refresh job's summary.
        """
        # Imported here: shared is below all of these in the dependency order,
        # and a module-level import would make services.location impossible to
        # import from any of them.
        from accounts.models import UserProfile
        from organization.models import OrganizationLocation
        from posts.models import Post
        from recruitments.models import Recruitment

        coords = {
            "latitude": location.latitude,
            "longitude": location.longitude,
        }

        return {
            "user_profiles": UserProfile.objects.filter(
                location_id=location.id
            ).update(**coords),
            "posts": Post.objects.filter(
                location_id=location.id
            ).update(**coords),
            "recruitments": Recruitment.objects.filter(
                location_id=location.id
            ).update(**coords),
            "organization_locations": OrganizationLocation.objects.filter(
                location_id=location.id
            ).update(**coords),
        }

    @staticmethod
    def build_denormalized(location: Location) -> dict:
        """Return data for Post/User fields"""
        return {
            "location": location,
            "location_name": location.name,
            "city": location.city,
            "country_code": location.country_code,
            "latitude": location.latitude,
            "longitude": location.longitude,
        }
