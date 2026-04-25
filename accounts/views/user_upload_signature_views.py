from core.views.base_views import BaseAPIView
from rest_framework.permissions import IsAuthenticated
from utils.response import response_data
from services.storage.factory import get_storage_service


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
    }

    def get(self, request):
        try:
            upload_type = request.query_params.get("type")
            count = int(request.query_params.get("count", 1))

            if upload_type not in self.ALLOWED_TYPES:
                return response_data(
                    False,
                    error="Invalid upload type",
                    status_code=400
                )

            if count < 1 or count > 10:
                return response_data(
                    False,
                    error="Invalid count (1-10 allowed)",
                    status_code=400
                )

            actor = self.actor

            # -----------------------------------
            # Prevent wrong actor usage
            # -----------------------------------
            if upload_type in {"profile", "cover", "posts"} and not actor.is_user:
                return response_data(
                    False,
                    error="Switch to personal account for this upload",
                    status_code=403
                )

            if upload_type in {
                "organization_logo",
                "organization_cover"
            } and not actor.is_org:
                return response_data(
                    False,
                    error="Switch to organization account for this upload",
                    status_code=403
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
            return response_data(
                False,
                error=str(ve),
                status_code=400
            )

        except Exception as e:
            return response_data(
                False,
                error=f"Failed to generate upload config: {str(e)}",
                status_code=500
            )