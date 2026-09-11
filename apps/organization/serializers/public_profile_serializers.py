"""
What a logged-out visitor is allowed to see of an organization profile.

Same allow-list discipline as the user side (see
``accounts/serializers/public_profile_serializers``) and for the same reason:
``OrganizationFullSerializer`` is a ModelSerializer over a model that will keep
growing, and this payload must not grow with it.

Locations ARE sent in full, coordinates included — unlike a user's. An org
location is a business address: a club's ground, an academy's campus. It is
already on the club's website and a map pin is the point of it. Nothing here
belongs to a private individual.
"""

from rest_framework import serializers


class PublicOrgSportSerializer(serializers.Serializer):
    """
    One row from ``OrganizationSport``.

    ``id`` is the SPORT's catalog id, not the join row's — it is the same value
    the authenticated payload sends and the client uses it as a render key.
    Nothing about it is private; the sport list is public reference data.
    """

    id = serializers.UUIDField(source="sport.id")
    name = serializers.CharField(source="sport.name")
    icon_name = serializers.CharField(source="sport.icon_name")
    icon_url = serializers.CharField(source="sport.icon_url")
    is_primary = serializers.BooleanField()


class PublicOrgLocationSerializer(serializers.Serializer):
    """One row from ``OrganizationLocation`` — a business address."""

    id = serializers.UUIDField()
    name = serializers.CharField()
    address = serializers.CharField()
    city = serializers.CharField()
    state = serializers.CharField()
    country_code = serializers.CharField()
    latitude = serializers.FloatField(allow_null=True)
    longitude = serializers.FloatField(allow_null=True)
    is_primary = serializers.BooleanField()


class PublicOrganizationProfileSerializer(serializers.Serializer):
    """
    The header block of a public organization profile.

    Expects ``profile`` joined and ``sports__sport`` / ``locations``
    prefetched.
    """

    id = serializers.UUIDField()
    username = serializers.CharField()
    name = serializers.CharField()
    type = serializers.CharField()
    is_verified = serializers.BooleanField()
    created_at = serializers.DateTimeField()

    logo = serializers.SerializerMethodField()
    cover_image = serializers.SerializerMethodField()
    headline = serializers.SerializerMethodField()
    description = serializers.SerializerMethodField()
    website = serializers.SerializerMethodField()
    level = serializers.SerializerMethodField()

    followers_count = serializers.SerializerMethodField()
    following_count = serializers.SerializerMethodField()
    posts_count = serializers.SerializerMethodField()

    sports = serializers.SerializerMethodField()
    locations = serializers.SerializerMethodField()

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

    def get_following_count(self, obj):
        profile = self._profile(obj)
        return profile.following_count if profile else 0

    def get_posts_count(self, obj):
        profile = self._profile(obj)
        return profile.posts_count if profile else 0

    def get_sports(self, obj):
        return PublicOrgSportSerializer(obj.sports.all(), many=True).data

    def get_locations(self, obj):
        return PublicOrgLocationSerializer(obj.locations.all(), many=True).data


# The exact key set the public org payload may contain — see the user twin.
PUBLIC_ORG_PROFILE_KEYS = {
    "id",
    "username",
    "name",
    "type",
    "is_verified",
    "created_at",
    "logo",
    "cover_image",
    "headline",
    "description",
    "website",
    "level",
    "followers_count",
    "following_count",
    "posts_count",
    "sports",
    "locations",
}
