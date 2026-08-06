from rest_framework import serializers


class PublicProfileToggleSerializer(serializers.Serializer):
    """
    Body for both privacy toggles (user + organization).

    ``required=True`` on purpose: an absent key would otherwise default to False
    and silently hide a profile whose owner sent a malformed request.
    """

    is_public_profile = serializers.BooleanField(required=True)
