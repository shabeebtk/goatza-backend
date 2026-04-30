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

class OrganizationService:

    MAX_ORG_PER_USER = 3

    @staticmethod
    def can_create_organization(user):
        count = Organization.objects.filter(created_by=user).count()
        return count < OrganizationService.MAX_ORG_PER_USER
    

    def generate_unique_org_username(name: str) -> str:
        """
        Generate unique organization username from name.
        Example:
        Real Madrid CF -> realmadridcf17
        """
        # lowercase + keep only a-z0-9
        base = re.sub(r"[^a-z0-9]", "", name.lower())

        # fallback safety
        if not base:
            base = "org"

        # keep max base length safe
        base = base[:20]

        username = f"{base}{random.randint(10,99)}"

        while Organization.objects.filter(username=username).exists():
            username = f"{base}{random.randint(1000,9999)}"

        return username

    @staticmethod
    @transaction.atomic
    def create_organization(user, data):
        """
        data:
        {
            name,
            username,
            type
        }
        """
        try:
            # LIMIT CHECK
            if not OrganizationService.can_create_organization(user):
                return False, "You can create up to 3 organizations only"

            username = OrganizationService.generate_unique_org_username(data['name'])

            if not username:
                return False, "Username is required"

            # UNIQUE CHECK
            if Organization.objects.filter(username=username).exists():
                return False, "Username already taken"

            # CREATE ORGANIZATION
            org = Organization.objects.create(
                name=data["name"],
                username=username,
                type=data["type"],
                created_by=user
            )

            # PROFILE
            profile = OrganizationProfile.objects.create(
                organization=org,
                headline=data.get("headline", ""),
                website=data.get("website", ""),
                logo=data.get("logo", ""),
                description=data.get("description", ""),
                level=data.get("level", ""),
            )

            # OWNER MEMBER
            OrganizationMember.objects.create(
                organization=org,
                user=user,
                role=OrganizationMember.Role.OWNER
            )

            # LOCATION (optional)
            location = data.get('location', {})
            if location and isinstance(location, dict) and location.get('city'):
                OrganizationLocation.objects.create(
                    organization=org,
                    name=location.get("name", ""),
                    address=location.get("address", ""),
                    city=location.get("city", ""),
                    state=location.get("state", ""),
                    country_code=location.get("country_code", ""),
                    latitude=location.get("latitude"),
                    longitude=location.get("longitude"),
                    is_primary=True
                )

            # SPORTS (optional)
            sport_ids = data.get("sport_ids", [])

            if sport_ids:
                sports = Sport.objects.filter(id__in=sport_ids)

                sport_objects = []
                first = True

                for sport in sports:
                    sport_objects.append(
                        OrganizationSport(
                            organization=org,
                            sport=sport,
                            is_primary=first
                        )
                    )
                    first = False

                OrganizationSport.objects.bulk_create(
                    sport_objects
                )

            logger.info(
                f"Organization created org={org.id} user={user.id}"
            )

            return True, {
                "id": str(org.id),
                "name": org.name,
                "username": org.username,
                "type": org.type,
                "headline": profile.headline,
                "logo": profile.logo,
            }

        except Exception as e:
            logger.error(f"Create organization error (user={user.id}): {str(e)}")
            return False, "Failed to create organization"
        


    @staticmethod
    def get_user_organizations(user):
        """
        List organizations where user is owner/member.
        """
        queryset = (
            Organization.objects.filter(
                members__user=user,
                is_active=True
            )
            .select_related("profile")
            .distinct()
            .order_by("name")
        )

        logger.info(f"Fetched organizations for user={user.id}")

        return queryset
    

    @staticmethod
    def get_organization(id=None, username=None):
        """
        Fetch organization by id OR username

        Args:
            id: UUID (organization_id)
            username: str

        Returns:
            Organization instance or None
        """

        queryset = (
            Organization.objects
            .filter(is_active=True)
            .select_related("profile")
            .prefetch_related(
                "locations",
                "sports__sport"
            )
        )

        if id:
            return queryset.filter(id=id).first()

        if username:
            return queryset.filter(username=username).first()

        return None