from rest_framework import serializers
from organization.models import Organization, OrganizationProfile
from utils.validations import validate_username_format


class UpdateOrganizationSerializer(serializers.Serializer):
    # Organization fields
    name = serializers.CharField(required=False, max_length=255)
    username = serializers.CharField(required=False, max_length=50)
    type = serializers.ChoiceField(choices=Organization.Type.choices, required=False)

    # Profile fields
    headline = serializers.CharField(required=False, allow_blank=True, max_length=150)
    description = serializers.CharField(required=False, allow_blank=True)
    website = serializers.URLField(required=False, allow_blank=True, max_length=500)
    level = serializers.ChoiceField(
        choices=OrganizationProfile.Level.choices,
        required=False,
        allow_blank=True
    )

    def validate_name(self, value):
        if not value.strip():
            raise serializers.ValidationError("Name cannot be empty")
        return value

    def validate_username(self, value):
        value = value.strip().lower()
        org_id = self.context.get("org_id")
        
        try:
            value = validate_username_format(value)
        except ValueError as e:
            raise serializers.ValidationError(str(e))

        if Organization.objects.exclude(id=org_id).filter(username__iexact=value).exists():
            raise serializers.ValidationError("Username already taken")
        
        return value
