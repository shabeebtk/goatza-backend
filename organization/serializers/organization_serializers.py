from rest_framework import serializers
from organization.models import Organization

class OrganizationMiniSerializer(serializers.ModelSerializer):
    class Meta:
        model = Organization
        fields = ["id", "slug", "name", "logo"]