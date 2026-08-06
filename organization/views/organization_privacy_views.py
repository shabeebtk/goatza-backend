"""Privacy settings for an organization. Thin — the rules live in the service."""

import logging

from rest_framework.exceptions import NotFound, PermissionDenied

from accounts.serializers.privacy_serializers import (
    PublicProfileToggleSerializer,
)
from core.views.base_views import BaseAPIView
from organization.services.organization_privacy_service import (
    OrganizationPrivacyService,
)
from utils.response import response_data

logger = logging.getLogger(__name__)


class OrganizationPublicProfilePrivacyAPIView(BaseAPIView):
    """
    PATCH /organizations/privacy/public-profile   {"is_public_profile": bool}

    Acts on the ORG the caller is currently acting as (``?org_id=`` overrides,
    matching the other org endpoints). Owner/admin only — enforced in
    OrganizationPrivacyService, not here, so the same gate covers any future
    caller.
    """

    def patch(self, request):
        actor = request.actor

        org_id = request.query_params.get("org_id")
        if not org_id:
            if not actor.is_org:
                return response_data(
                    success=False,
                    message="organization request only or provide org_id",
                    status_code=400,
                )
            org_id = actor.organization.id

        serializer = PublicProfileToggleSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            value = OrganizationPrivacyService.set_public_profile(
                organization_id=org_id,
                user=request.user,
                is_public=serializer.validated_data["is_public_profile"],
            )
        except PermissionDenied as exc:
            return response_data(False, str(exc.detail), status_code=403)
        except NotFound as exc:
            return response_data(False, str(exc.detail), status_code=404)

        return response_data(
            success=True,
            message="Privacy updated",
            data={"is_public_profile": value},
        )
