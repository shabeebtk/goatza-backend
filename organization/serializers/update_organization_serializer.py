from rest_framework import serializers
from organization.models import Organization, OrganizationProfile
from usernames.services.username_service import UsernameService
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
        """
        Format + availability against the SHARED namespace.

        Checking Organization alone was the other half of the collision: it let
        an org take a handle a user already held. A friendly pre-check only —
        the arbiter is the unique constraint UsernameService.claim writes
        against, so the view handles UsernameTaken too.
        """
        org_id = self.context.get("org_id")
        organization = (
            Organization.objects.filter(id=org_id).first() if org_id else None
        )

        try:
            available = UsernameService.is_available(
                value, exclude_org=organization
            )
        except ValueError as e:
            # Malformed / reserved — a different answer from "taken". Dots land
            # here now: the charset no longer allows them for anyone.
            raise serializers.ValidationError(str(e))

        if not available:
            raise serializers.ValidationError("Username already taken")

        # The NORMALIZED value, not the input.
        return validate_username_format(value)
