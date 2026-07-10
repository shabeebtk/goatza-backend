from rest_framework import serializers

from accounts.models import User
from organization.models import Organization


class ExploreUserSerializer(serializers.ModelSerializer):
    """Lightweight player card for the explore feed."""

    name = serializers.CharField(source="profile.name", read_only=True)
    headline = serializers.CharField(source="profile.headline", read_only=True)
    profile_photo = serializers.URLField(source="profile.profile_photo", read_only=True)
    city = serializers.CharField(source="profile.city", read_only=True)
    followers_count = serializers.IntegerField(
        source="profile.followers_count", read_only=True
    )
    # Present only in "nearby" mode; null in "popular" mode.
    distance_km = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = (
            "id",
            "name",
            "username",
            "role",
            "headline",
            "profile_photo",
            "city",
            "followers_count",
            "distance_km",
        )

    def get_distance_km(self, obj):
        value = getattr(obj, "distance_km", None)
        return round(value, 2) if value is not None else None


class ExploreOrgSerializer(serializers.ModelSerializer):
    """Lightweight organization card for the explore feed."""

    logo = serializers.SerializerMethodField()
    headline = serializers.SerializerMethodField()
    level = serializers.SerializerMethodField()
    followers_count = serializers.SerializerMethodField()
    # City comes from the primary location, annotated by the service.
    city = serializers.SerializerMethodField()
    # Present only in "nearby" mode; null in "popular" mode.
    distance_km = serializers.SerializerMethodField()

    class Meta:
        model = Organization
        fields = (
            "id",
            "name",
            "username",
            "type",
            "is_verified",
            "logo",
            "headline",
            "level",
            "city",
            "followers_count",
            "distance_km",
        )

    def _profile(self, obj):
        # Reverse OneToOne raises an AttributeError subclass when missing, so
        # getattr's default kicks in cleanly.
        return getattr(obj, "profile", None)

    def get_logo(self, obj):
        profile = self._profile(obj)
        return profile.logo if profile else ""

    def get_headline(self, obj):
        profile = self._profile(obj)
        return profile.headline if profile else ""

    def get_level(self, obj):
        profile = self._profile(obj)
        return profile.level if profile else ""

    def get_followers_count(self, obj):
        profile = self._profile(obj)
        return profile.followers_count if profile else 0

    def get_city(self, obj):
        return getattr(obj, "city", "") or ""

    def get_distance_km(self, obj):
        value = getattr(obj, "distance_km", None)
        return round(value, 2) if value is not None else None
