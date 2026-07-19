from core.views.base_views import BaseAPIView
from rest_framework.permissions import IsAuthenticated
from utils.response import response_data
from utils.errors import error_body
from services.storage.factory import get_storage_service
from organization.models import Organization
from organization.services.organization_member_service import OrganizationMemberService

class GetUploadConfigAPIView(BaseAPIView):
    """
    Uses request.actor

    Headers:
    X-Actor-Type: user | organization
    X-Actor-Id: <org_id>   (required when organization)
    """

    ALLOWED_TYPES = {
        "profile",
        "cover",
        "posts",
        "organization_logo",
        "organization_cover",
        "recruitments",
        # Chat media — works for both user and org actors (no actor-type guard
        # below), scoped server-side to chat/<actor path>.
        "chat",
    }

    def get(self, request):
        try:
            upload_type = request.query_params.get("type")
            org_id = request.query_params.get("org_id")

            try:
                count = int(request.query_params.get("count", 1))
            except (TypeError, ValueError):
                count = 0

            if upload_type not in self.ALLOWED_TYPES:
                msg = "Invalid upload type"
                return response_data(
                    success=False,
                    message=msg,
                    error=msg,
                    status_code=400,
                    data=error_body(msg, "type")
                )

            if count < 1 or count > 10:
                msg = "Invalid count (1-10 allowed)"
                return response_data(
                    success=False,
                    message=msg,
                    error=msg,
                    status_code=400,
                    data=error_body(msg, "count")
                )

            actor = self.actor
            user = request.user
            if org_id: # for user want to access org directly
                try:
                    org = Organization.objects.select_related("profile").get(id=org_id)
                    if not OrganizationMemberService.is_organization_member(org, user):
                        msg = "You are not a member of this organization"
                        return response_data(
                            success=False,
                            message=msg,
                            error=msg,
                            status_code=400,
                            data=error_body(msg, "org_id")
                        )

                    actor.organization = org
                    actor.actor_type = "organization"

                except Organization.DoesNotExist:
                    msg = "Organization not found"
                    return response_data(
                        success=False,
                        message=msg,
                        error=msg,
                        status_code=404,
                        data=error_body(msg, "org_id")
                    )

            # -----------------------------------
            # Prevent wrong actor usage
            # -----------------------------------
            if upload_type in {"profile", "cover"} and not actor.is_user:
                msg = "Switch to your personal account for this upload"
                return response_data(
                    success=False,
                    message=msg,
                    error=msg,
                    status_code=403,
                    data=error_body(msg, "type")
                )

            if upload_type in {
                "organization_logo",
                "organization_cover",
                "recruitments"
            } and not actor.is_org:
                msg = "Switch to your organization account for this upload"
                return response_data(
                    success=False,
                    message=msg,
                    error=msg,
                    status_code=403,
                    data=error_body(msg, "type")
                )

            storage = get_storage_service()

            config = storage.get_upload_config(
                actor=actor,
                upload_type=upload_type,
                count=count
            )

            return response_data(
                success=True,
                data=config
            )

        except ValueError as ve:
            msg = str(ve) or "Invalid upload request"
            return response_data(
                success=False,
                message=msg,
                error=msg,
                status_code=400,
                data=error_body(msg)
            )

        except Exception as e:
            return response_data(
                success=False,
                message="Failed to generate upload config",
                status_code=500,
                error=str(e)
            )