from django.db import transaction
from django.db.models import Q
from shared.models import Location


class LocationService:

    @staticmethod
    def normalize_data(data: dict) -> dict:
        """Normalize incoming location data"""
        return {
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
    @transaction.atomic
    def get_or_create_location(data: dict) -> Location:
        data = LocationService.normalize_data(data)

        lat = data["latitude"]
        lng = data["longitude"]
        external_id = data["external_id"]

        LocationService.validate(lat, lng)

        # 🔹 1. Try external_id (best match)
        if external_id:
            location = Location.objects.filter(external_id=external_id).first()
            if location:
                return location

        # 🔹 2. Try geo proximity match
        location = Location.objects.filter(
            latitude__range=(lat - 0.0005, lat + 0.0005),
            longitude__range=(lng - 0.0005, lng + 0.0005),
            name__iexact=data["name"]
        ).first()

        if location:
            return location

        # 🔹 3. Create safely (handle race condition)
        try:
            return Location.objects.create(**data)
        except Exception:
            # fallback if duplicate created in parallel
            return Location.objects.filter(
                Q(external_id=external_id) |
                Q(latitude=lat, longitude=lng, name=data["name"])
            ).first()

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