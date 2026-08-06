from rest_framework import serializers
from accounts.models import User, UserProfile
from highlights.selectors.highlight_selectors import visible_highlights_count
from sports.serializers.user_sports_serializers import UserSportMiniSerializer

class BaseUserSerializer(serializers.ModelSerializer):
    name = serializers.CharField(source='profile.name', read_only=True)
    profile_photo = serializers.URLField(source='profile.profile_photo', read_only=True)

    class Meta:
        model = User
        fields = [
            'id',
            'username',
            'email',
            'role',
            'is_role_confirmed',
            'is_onboarding_completed',
            'name',
            'profile_photo',
            'is_email_verified'
        ]

class UserSerializer(BaseUserSerializer):
    pass

class UserMiniSerializer(serializers.ModelSerializer):
    name = serializers.CharField(source='profile.name', read_only=True)
    profile_photo = serializers.URLField(source='profile.profile_photo', read_only=True)
    headline = serializers.CharField(source='profile.headline', read_only=True)

    class Meta:
        model = User
        fields = [
            'id',
            'username',
            'name',
            'profile_photo',
            'headline',
        ]

class UserFullSerializer(BaseUserSerializer):
    cover_photo = serializers.URLField(source='profile.cover_photo', read_only=True)
    headline = serializers.CharField(source='profile.headline', read_only=True)
    about = serializers.CharField(source='profile.about', read_only=True)
    followers_count = serializers.CharField(source='profile.followers_count', read_only=True)
    following_count = serializers.CharField(source='profile.following_count', read_only=True)
    connections_count = serializers.CharField(source='profile.connections_count', read_only=True)
    height_cm = serializers.CharField(source='profile.height_cm', read_only=True)
    weight_kg = serializers.CharField(source='profile.weight_kg', read_only=True)
    gender = serializers.CharField(source='profile.gender', read_only=True)
    birthdate = serializers.DateField(source='profile.birthdate', read_only=True, allow_null=True)
    primary_sport = serializers.SerializerMethodField()
    location = serializers.SerializerMethodField()
    highlights_count = serializers.SerializerMethodField()
    # The owner's own privacy setting, so the Settings screen can render the
    # toggle without a second request. Safe here and ONLY here: this serializer
    # is behind IsAuthenticated and already carries the email — it must never
    # be reused for public output (see public_profile_serializers.py).
    is_public_profile = serializers.BooleanField(
        source='profile.is_public_profile', read_only=True
    )

    class Meta(BaseUserSerializer.Meta):
        fields = BaseUserSerializer.Meta.fields + [
            'highlights_count',
            'is_public_profile',
            'cover_photo',
            'headline',
            'about',
            'followers_count',
            'following_count',
            'connections_count',
            'height_cm',
            'weight_kg',
            'gender',
            'birthdate',
            'created_at',
            'primary_sport',
            'location'
        ]

    def get_highlights_count(self, obj):
        """
        How many of this player's highlights the VIEWER may see — the number
        behind the "▶ Highlights (n)" chip, so the profile page does not need a
        second request.

        Costs one COUNT, and only when the caller puts an ``actor`` in the
        serializer context. Bulk/list callers leave it out (the field comes back
        null) so a page of profiles never fans out into one count per row.
        """
        actor = self.context.get("actor")

        if actor is None:
            return None

        return visible_highlights_count(obj, actor)

    def get_primary_sport(self, obj):
        primary = obj.sports.filter(is_primary=True).first()

        if not primary:
            return None

        positions = obj.positions.filter(
            sport=primary.sport,
            is_primary=True
        ).first()

        return {
            "sport": primary.sport.name,
            "icon_name": primary.sport.icon_name,
            "icon_url": primary.sport.icon_url,
            "experience_level": primary.experience_level,
            "primary_position": positions.position.name if positions else None
        }

    def get_location(self, obj):
        profile = obj.profile

        if not profile or not profile.latitude:
            return None

        return {
            "name": profile.location_name,
            "city": profile.city,
            "country_code": profile.country_code,
            "latitude": profile.latitude,
            "longitude": profile.longitude,
        }



class UpdateUserMediaSerializer(serializers.Serializer):
    profile_photo = serializers.URLField(required=False)
    profile_photo_public_id = serializers.CharField(required=False)

    cover_photo = serializers.URLField(required=False)
    cover_photo_public_id = serializers.CharField(required=False)

    def validate(self, data):
        if not data:
            raise serializers.ValidationError("No data provided")

        # Ensure pair consistency
        if "profile_photo" in data and "profile_photo_public_id" not in data:
            raise serializers.ValidationError("Profile public_id required")

        if "cover_photo" in data and "cover_photo_public_id" not in data:
            raise serializers.ValidationError("Cover public_id required")

        return data