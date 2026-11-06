from rest_framework import serializers
from shared.models import Location
from apps.organization.models import (
    Organization, OrganizationProfile, OrganizationLocation, OrganizationSport,
    OrganizationMember
)
from apps.organization.permissions import member_permissions

class OrganizationLocationInputSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=255, required=False, allow_blank=True)
    address = serializers.CharField(max_length=500, required=False, allow_blank=True)
    city = serializers.CharField(max_length=100, required=False, allow_blank=True)
    state = serializers.CharField(max_length=100, required=False, allow_blank=True)
    country_code = serializers.CharField(max_length=5, required=False, allow_blank=True)
    latitude = serializers.FloatField(required=False, allow_null=True)
    longitude = serializers.FloatField(required=False, allow_null=True)

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


class MyMembershipSerializer(serializers.Serializer):
    """
    my_role + my_permissions: the REQUESTING user in this org, null and [] if
    they aren't a member.

    A hint for the UI and nothing more — the real gates are server-side in
    apps/organization/permissions.py.

    Fed in one of two ways. A list passes ``memberships_by_org`` from the context
    (OrganizationService.get_user_memberships), so a page of orgs costs one query
    and not one per row. A single org passes ``request`` and costs one lookup,
    shared by both fields.
    """

    my_role = serializers.SerializerMethodField()
    my_permissions = serializers.SerializerMethodField()

    def _my_membership(self, obj):
        memberships = self.context.get("memberships_by_org")
        if memberships is not None:
            return memberships.get(obj.id)

        request = self.context.get("request")
        user = getattr(request, "user", None)
        if not user or not user.is_authenticated:
            return None

        if getattr(self, "_my_membership_org_id", None) != obj.id:
            self._my_membership_org_id = obj.id
            self._my_membership_row = OrganizationMember.objects.filter(
                organization=obj,
                user=user,
                status=OrganizationMember.Status.ACTIVE,
            ).first()

        return self._my_membership_row

    def get_my_role(self, obj):
        member = self._my_membership(obj)
        return member.role if member else None

    def get_my_permissions(self, obj):
        return sorted(member_permissions(self._my_membership(obj)))


class MyOrganizationSerializer(MyMembershipSerializer, OrganizationMiniSerializer):
    """
    An org the way /organizations/list shows it to one of its own members.

    A subclass, not two more fields on OrganizationMiniSerializer: that one is
    nested in post, comment and recruitment payloads, which inherit the request
    from their parent, so fields on it would mean one membership query per row of
    every feed.
    """

    class Meta(OrganizationMiniSerializer.Meta):
        fields = OrganizationMiniSerializer.Meta.fields + (
            "my_role",
            "my_permissions",
        )



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
class OrganizationFullSerializer(MyMembershipSerializer, serializers.ModelSerializer):
    logo = serializers.SerializerMethodField()
    cover_image = serializers.SerializerMethodField()
    headline = serializers.SerializerMethodField()
    description = serializers.SerializerMethodField()
    website = serializers.SerializerMethodField()
    level = serializers.SerializerMethodField()

    followers_count = serializers.SerializerMethodField()
    following_count = serializers.SerializerMethodField()
    posts_count = serializers.SerializerMethodField()

    is_public_profile = serializers.SerializerMethodField()

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

            # The org's calendar. The settings screen renders the picker from
            # it and the recruitment wizard pre-fills a new posting with it —
            # a plain column on the row already fetched, so no extra query.
            "timezone",

            "logo",
            "cover_image",
            "headline",
            "description",
            "website",
            "level",

            "followers_count",
            "following_count",
            "posts_count",

            "is_public_profile",
            "my_role",
            "my_permissions",

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
    
    def get_following_count(self, obj):
        profile = self._profile(obj)
        return profile.following_count if profile else 0

    def get_posts_count(self, obj):
        profile = self._profile(obj)
        return profile.posts_count if profile else 0

    def get_is_public_profile(self, obj):
        """
        Whether logged-out visitors can see this org's profile.

        Not a secret — the answer is observable by opening the public URL — and
        the settings screen needs it to render the toggle in the right state.
        """
        profile = self._profile(obj)
        return profile.is_public_profile if profile else True



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