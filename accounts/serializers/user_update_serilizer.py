from rest_framework import serializers
from django.utils import timezone
from accounts.models import User, UserProfile
from utils.validations import validate_username_format


class UpdateUserProfileSerializer(serializers.Serializer):
    # User
    username = serializers.CharField(required=False, max_length=50)

    # Profile
    name = serializers.CharField(required=False)  # required but not blank
    headline = serializers.CharField(required=False, allow_blank=True)
    about = serializers.CharField(required=False, allow_blank=True)

    gender = serializers.ChoiceField(
        choices=UserProfile.Gender.choices,
        required=False,
        allow_blank=True
    )

    birthdate = serializers.DateField(required=False, allow_null=True)

    height_cm = serializers.IntegerField(required=False, allow_null=True)
    weight_kg = serializers.DecimalField(
        max_digits=5,
        decimal_places=2,
        required=False,
        allow_null=True
    )

    # location 
    location = serializers.DictField(required=False, allow_null=True)

    # VALIDATIONS

    def validate_username(self, value):
        user = self.context["request"].user
        value = value.strip().lower()

        # format validation (your function)
        try:
            value = validate_username_format(value)
        except ValueError as e:
            raise serializers.ValidationError(str(e))

        # uniqueness check
        if User.objects.exclude(id=user.id).filter(username__iexact=value).exists():
            raise serializers.ValidationError("Username already taken")
        return value
     

    def validate_name(self, value):
        if not value.strip():
            raise serializers.ValidationError("Name cannot be empty")
        return value

    def validate_birthdate(self, value):
        if value and value > timezone.now().date():
            raise serializers.ValidationError("Birthdate cannot be in the future")
        return value

    def validate_height_cm(self, value):
        if value is not None and (value < 50 or value > 300):
            raise serializers.ValidationError("Height must be between 50 and 300 cm")
        return value

    def validate_weight_kg(self, value):
        if value is not None and (value < 20 or value > 300):
            raise serializers.ValidationError("Weight must be between 20 and 300 kg")
        return value

    def validate_location(self, value):
        if value is None:
            return value

        required_fields = ["latitude", "longitude", "name"]

        for field in required_fields:
            if field not in value:
                raise serializers.ValidationError(f"{field} is required")

        return value