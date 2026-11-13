"""
Who may change an organization's privacy settings, and what changing one does.

The role gate lives here rather than in the view because hiding the toggle in
the UI is not a permission — a coach with a token can PATCH the endpoint
directly. Every caller, now and later, goes through this one check.
"""

import logging

from rest_framework.exceptions import NotFound, PermissionDenied

from apps.organization.models import Organization, OrganizationMember
from apps.organization.permissions import (
    DENIED_MESSAGES,
    OrgPermission,
    has_org_permission,
)
from apps.squads.services.public_cache import bust_public_squads
from utils.cache import cache_delete
from utils.cache_keys import CacheKeys

logger = logging.getLogger(__name__)


class OrganizationPrivacyService:

    @staticmethod
    def _require_privacy_role(organization, user):
        """
        Raise unless ``user`` holds the org.privacy permission in ``organization``.

        Coaches and staff run the day-to-day; making the club invisible to the
        public web is an ownership decision.

        NotFound (not PermissionDenied) for a non-member: someone who is not in
        the org at all should not learn that an org id is real by probing this.
        A member with the wrong role DOES get a 403 — they already know the org
        exists, and a precise error is what makes the disabled row explicable.
        """
        membership = OrganizationMember.objects.filter(
            organization=organization,
            user=user,
            status=OrganizationMember.Status.ACTIVE,
        ).first()

        if membership is None:
            raise NotFound("Organization not found")

        if not has_org_permission(membership, OrgPermission.ORG_PRIVACY):
            raise PermissionDenied(DENIED_MESSAGES[OrgPermission.ORG_PRIVACY.value])

        return membership

    @staticmethod
    def set_public_profile(organization_id, user, is_public):
        """
        Flip the org's logged-out visibility. Returns the saved value.

        ``organization_id`` comes from the acting org actor (or an explicit
        ?org_id=), and membership is re-verified here regardless — resolve_actor
        proves membership, not role.
        """
        organization = (
            Organization.objects
            .select_related("profile")
            .filter(id=organization_id)
            .first()
        )

        if organization is None:
            raise NotFound("Organization not found")

        OrganizationPrivacyService._require_privacy_role(organization, user)

        profile = getattr(organization, "profile", None)
        if profile is None:
            raise NotFound("Organization profile not found")

        profile.is_public_profile = is_public
        profile.save(update_fields=["is_public_profile", "updated_at"])

        # An org that just went private must stop being served from the public
        # bundle cache on the very next request.
        cache_delete(CacheKeys.public_org_profile(organization.username))
        # The anonymous squad pages are gated on the same setting.
        bust_public_squads(organization.id)

        logger.info(
            "[ORG PRIVACY] org=%s user=%s is_public_profile=%s",
            organization.id, user.id, is_public,
        )

        return is_public
