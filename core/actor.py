from rest_framework.exceptions import PermissionDenied
from organization.models import OrganizationMember
from utils.validations import is_valid_uuid

class Actor:
    def __init__(self, actor_type, user=None, organization=None, organization_member=None):
        self.actor_type = actor_type
        self.user = user
        self.organization = organization
        # The OrganizationMember the logged-in user acts through (org actors
        # only). Used to stamp created_by/reviewed_by/changed_by audit FKs.
        self.organization_member = organization_member

    @property
    def is_user(self):
        return self.actor_type == "user"

    @property
    def is_org(self):
        return self.actor_type == "organization"


def resolve_actor(request):
    user = request.user
    if not user or not user.is_authenticated:
        return None
    
    actor_type = request.headers.get("X-Actor-Type", "user")
    org_id = request.headers.get("X-Actor-Id")

    if actor_type == "user":
        return Actor(actor_type="user", user=user)


    if actor_type == "organization":
        if not org_id or not is_valid_uuid(org_id):
            raise PermissionDenied("Organization ID Not found or Invalid")

        membership = OrganizationMember.objects.filter(
            user=user,
            organization_id=org_id
        ).select_related("organization").first()

        if not membership:
            raise PermissionDenied("You are not part of this organization")

        return Actor(
            actor_type="organization",
            organization=membership.organization,
            organization_member=membership
        )

    raise PermissionDenied("Invalid actor type")