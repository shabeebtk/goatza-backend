from rest_framework import serializers

from shared.models import Location

class UpsertOrganizationLocationSerializer(serializers.Serializer):
    id = serializers.UUIDField(required=False, allow_null=True)
    name = serializers.CharField(max_length=255, required=False, allow_blank=True)
    address = serializers.CharField(max_length=500, required=False, allow_blank=True)
    city = serializers.CharField(max_length=100)
    state = serializers.CharField(max_length=100, required=False, allow_blank=True)
    country_code = serializers.CharField(max_length=5)
    latitude = serializers.FloatField(required=False, allow_null=True)
    longitude = serializers.FloatField(required=False, allow_null=True)
    is_primary = serializers.BooleanField(default=False)

    # PLACE (docs/PLACES_MIGRATION.md 5.4). ``name`` above is the BRANCH label
    # ("Main Branch"); the place's own label is ``location_name``, the same word
    # UserProfile and Post use for it. provider + external_id are the shared
    # Location's identity — without them every branch mints a duplicate row.
    provider = serializers.ChoiceField(
        choices=Location.Provider.choices,
        required=False,
        default=Location.Provider.GOOGLE,
    )
    external_id = serializers.CharField(
        max_length=255, required=False, allow_blank=True
    )
    location_name = serializers.CharField(
        max_length=255, required=False, allow_blank=True
    )
    type = serializers.ChoiceField(
        choices=Location.Type.choices,
        required=False,
        default=Location.Type.CITY,
    )
    country = serializers.CharField(
        max_length=100, required=False, allow_blank=True
    )

    def validate(self, attrs):
        latitude = attrs.get("latitude")
        longitude = attrs.get("longitude")

        if latitude is not None and not (-90 <= latitude <= 90):
            raise serializers.ValidationError({"latitude": "Latitude must be between -90 and 90"})

        if longitude is not None and not (-180 <= longitude <= 180):
            raise serializers.ValidationError({"longitude": "Longitude must be between -180 and 180"})

        return attrs