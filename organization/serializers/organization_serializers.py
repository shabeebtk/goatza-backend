from rest_framework import serializers
from organization.models import (
    Organization, OrganizationProfile, OrganizationLocation, OrganizationSport
)

class OrganizationLocationInputSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=255, required=False, allow_blank=True)
    address = serializers.CharField(max_length=500, required=False, allow_blank=True)
    city = serializers.CharField(max_length=100, required=False, allow_blank=True)
    state = serializers.CharField(max_length=100, required=False, allow_blank=True)
    country_code = serializers.CharField(max_length=5, required=False, allow_blank=True)
    latitude = serializers.FloatField(required=False)
    longitude = serializers.FloatField(required=False)

    def validate(self, attrs):
        latitude = attrs.get("latitude")
        longitude = attrs.get("longitude")

        if latitude is not None and not (-90 <= latitude <= 90):
            raise serializers.ValidationError(
                {"latitude": "Latitude must be between -90 and 90"}
            )

        if longitude is not None and not (-180 <= longitude <= 180):
            raise serializers.ValidationError(
                {"longitude": "Longitude must be between -180 and 180"}
            )

        return attrs



class OrganizationCreateSerializer(serializers.Serializer):
    # required
    name = serializers.CharField(max_length=100)
    type = serializers.ChoiceField(choices=Organization.Type.choices)

    # optional profile
    headline = serializers.CharField(max_length=150, required=False, allow_blank=True)
    website = serializers.URLField(required=False, allow_blank=True)
    logo = serializers.URLField(required=False, allow_blank=True)
    description = serializers.CharField(required=False, allow_blank=True)
    level = serializers.ChoiceField(
        choices=OrganizationProfile.Level.choices,
        required=False,
        allow_blank=True
    )
    # optional location
    location = OrganizationLocationInputSerializer(required=False)
    # optional sports ids
    sport_ids = serializers.ListField(
        child=serializers.UUIDField(),
        required=False,
        allow_empty=True
    )

class OrganizationMiniSerializer(serializers.ModelSerializer):
    logo = serializers.SerializerMethodField()
    headline = serializers.SerializerMethodField()

    class Meta:
        model = Organization
        fields = (
            "id",
            "name",
            "username",
            "type",
            "logo",
            "headline",
            "is_verified",
        )

    def get_logo(self, obj):
        if hasattr(obj, "profile") and obj.profile.logo:
            return obj.profile.logo
        return ""

    def get_headline(self, obj):
        if hasattr(obj, "profile") and obj.profile.headline:
            return obj.profile.headline
        return ""
    




class OrganizationLocationSerializer(serializers.ModelSerializer):
    class Meta:
        model = OrganizationLocation
        fields = (
            "id",
            "name",
            "address",
            "city",
            "state",
            "country_code",
            "latitude",
            "longitude",
            "is_primary",
        )


# -----------------------------------
# SPORT
# -----------------------------------
class OrganizationSportSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(source="sport.id")
    name = serializers.CharField(source="sport.name")
    icon_name = serializers.CharField(source="sport.icon_name")
    icon_url = serializers.CharField(source="sport.icon_url")

    class Meta:
        model = OrganizationSport
        fields = (
            "id",
            "name",
            "icon_name",
            "icon_url",
            "is_primary",
        )


# -----------------------------------
# FULL
# -----------------------------------
class OrganizationFullSerializer(serializers.ModelSerializer):
    logo = serializers.SerializerMethodField()
    cover_image = serializers.SerializerMethodField()
    headline = serializers.SerializerMethodField()
    description = serializers.SerializerMethodField()
    website = serializers.SerializerMethodField()
    level = serializers.SerializerMethodField()

    followers_count = serializers.SerializerMethodField()
    posts_count = serializers.SerializerMethodField()

    locations = OrganizationLocationSerializer(
        many=True,
        read_only=True
    )

    sports = OrganizationSportSerializer(
        many=True,
        read_only=True
    )

    class Meta:
        model = Organization
        fields = (
            "id",
            "name",
            "username",
            "type",
            "is_verified",

            "logo",
            "cover_image",
            "headline",
            "description",
            "website",
            "level",

            "followers_count",
            "posts_count",

            "locations",
            "sports",

            "created_at",
        )

    def _profile(self, obj):
        return getattr(obj, "profile", None)

    def get_logo(self, obj):
        profile = self._profile(obj)
        return profile.logo if profile else ""

    def get_cover_image(self, obj):
        profile = self._profile(obj)
        return profile.cover_image if profile else ""

    def get_headline(self, obj):
        profile = self._profile(obj)
        return profile.headline if profile else ""

    def get_description(self, obj):
        profile = self._profile(obj)
        return profile.description if profile else ""

    def get_website(self, obj):
        profile = self._profile(obj)
        return profile.website if profile else ""

    def get_level(self, obj):
        profile = self._profile(obj)
        return profile.level if profile else ""

    def get_followers_count(self, obj):
        profile = self._profile(obj)
        return profile.followers_count if profile else 0

    def get_posts_count(self, obj):
        profile = self._profile(obj)
        return profile.posts_count if profile else 0
    

# ---------------------------------------------- 
# media update 
class UpdateOrganizationMediaSerializer(serializers.Serializer):
    logo = serializers.URLField(required=False)
    logo_public_id = serializers.CharField(required=False)

    cover_image = serializers.URLField(required=False)
    cover_image_public_id = serializers.CharField(required=False)

    is_delete_logo = serializers.BooleanField(required=False, default=False)
    is_delete_cover = serializers.BooleanField(required=False, default=False)

    def validate(self, data):
        if not data:
            raise serializers.ValidationError("No data provided")

        # pair validation
        if "logo" in data and "logo_public_id" not in data:
            raise serializers.ValidationError("logo_public_id required")

        if "cover_image" in data and "cover_image_public_id" not in data:
            raise serializers.ValidationError("cover_image_public_id required")

        return data