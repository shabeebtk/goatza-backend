from rest_framework import serializers
from accounts.models import User, UserProfile
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
    primary_sport = serializers.SerializerMethodField()
    location = serializers.SerializerMethodField()

    class Meta(BaseUserSerializer.Meta):
        fields = BaseUserSerializer.Meta.fields + [
            'cover_photo',
            'headline',
            'about',
            'followers_count', 
            'following_count', 
            'connections_count',
            'height_cm',
            'weight_kg',
            'created_at',
            'primary_sport',
            'location'
        ]

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