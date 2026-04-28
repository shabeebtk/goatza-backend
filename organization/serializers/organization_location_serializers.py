from rest_framework import serializers

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

    def validate(self, attrs):
        latitude = attrs.get("latitude")
        longitude = attrs.get("longitude")

        if latitude is not None and not (-90 <= latitude <= 90):
            raise serializers.ValidationError({"latitude": "Latitude must be between -90 and 90"})

        if longitude is not None and not (-180 <= longitude <= 180):
            raise serializers.ValidationError({"longitude": "Longitude must be between -180 and 180"})

        return attrs