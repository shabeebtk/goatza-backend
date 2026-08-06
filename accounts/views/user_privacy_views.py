"""
Privacy settings the signed-in user owns.

Split out of user_views.py rather than bolted onto UpdateUserProfileAPIView:
the profile update returns a whole UserFullSerializer payload and re-runs the
username/location machinery, none of which a boolean toggle needs — and the
settings screen wants an optimistic PATCH that costs one UPDATE.
"""

import logging

from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from accounts.models import UserProfile
from accounts.serializers.privacy_serializers import (
    PublicProfileToggleSerializer,
)
from utils.cache import cache_delete
from utils.cache_keys import CacheKeys
from utils.response import response_data

logger = logging.getLogger(__name__)


class UserPublicProfilePrivacyAPIView(APIView):
    """
    PATCH /user/privacy/public-profile   {"is_public_profile": bool}

    Self only — there is no id in the URL, so a user can never flip anybody
    else's flag. The value governs the logged-out web view; in-app visibility
    is unchanged either way (see UserProfile.is_public_profile).
    """

    permission_classes = [IsAuthenticated]

    def patch(self, request):
        TAG = "[PRIVACY PUBLIC PROFILE]"
        user = request.user

        serializer = PublicProfileToggleSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        value = serializer.validated_data["is_public_profile"]

        try:
            profile = user.profile
        except UserProfile.DoesNotExist:
            return response_data(False, "Profile not found", status_code=404)

        profile.is_public_profile = value
        profile.save(update_fields=["is_public_profile", "updated_at"])

        # The public bundle is cached by username; a profile that just went
        # private must stop being served from that cache immediately.
        if user.username:
            cache_delete(CacheKeys.public_user_profile(user.username))

        logger.info(f"{TAG} user={user.id} is_public_profile={value}")

        return response_data(
            success=True,
            message="Privacy updated",
            data={"is_public_profile": value},
        )
