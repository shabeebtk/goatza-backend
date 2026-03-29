from rest_framework import serializers
from accounts.models import User, UserProfile

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

    class Meta:
        model = User
        fields = [
            'id',
            'username',
            'name',
            'profile_photo',
        ]

class UserFullSerializer(BaseUserSerializer):
    cover_photo = serializers.URLField(source='profile.cover_photo', read_only=True)
    headline = serializers.CharField(source='profile.headline', read_only=True)
    about = serializers.CharField(source='profile.about', read_only=True)

    class Meta(BaseUserSerializer.Meta):
        fields = BaseUserSerializer.Meta.fields + [
            'cover_photo',
            'headline',
            'about',
            'created_at'
        ]