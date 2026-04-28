import logging, re, random
from django.db import transaction
from sports.models import Sport
from organization.models import (
    Organization,
    OrganizationProfile,
    OrganizationMember,
    OrganizationLocation,
    OrganizationSport
)

logger = logging.getLogger(__name__)

class OrganizationMemberService:
    @staticmethod
    def is_organization_member(org, user):
        return OrganizationMember.objects.filter(
            organization=org,
            user=user
        ).exists()