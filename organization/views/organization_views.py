import logging
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from organization.services.organization_service import OrganizationService
from organization.serializers.organization_serializers import (
    OrganizationCreateSerializer, OrganizationMiniSerializer, OrganizationFullSerializer
)
from utils.response import response_data
from utils.validations import is_valid_uuid

logger = logging.getLogger(__name__)

class CreateOrganizationAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        try:
            serializer = OrganizationCreateSerializer(
                data=request.data
            )

            if not serializer.is_valid():
                return response_data(
                    success=False,
                    message="Invalid data",
                    data=serializer.errors,
                    status_code=400
                )

            success, result = OrganizationService.create_organization(
                user=request.user,
                data=serializer.validated_data
            )

            return response_data(
                success=success,
                message=(
                    "Organization created successfully"
                    if success else result
                ),
                data=result if success else {},
                status_code=201 if success else 400
            )

        except Exception as e:
            logger.error(
                f"CreateOrganizationAPIView error: {str(e)}"
            )

            return response_data(
                success=False,
                message="Something went wrong",
                status_code=500,
                error=str(e)
            )




class ListUserOrganizationsAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            organizations = OrganizationService.get_user_organizations(
                request.user
            )

            serializer = OrganizationMiniSerializer(
                organizations,
                many=True
            )

            return response_data(
                success=True,
                message="Organizations fetched successfully",
                data=serializer.data,
                status_code=200
            )

        except Exception as e:
            logger.error(
                f"UserOrganizationsAPIView error user={request.user.id}: {str(e)}"
            )

            return response_data(
                success=False,
                message="Failed to fetch organizations",
                status_code=500,
                error=str(e)
            )
        


class OrganizationsDetailsAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            view_type = request.GET.get("type", "mini").lower()
            organization_id = request.query_params.get('organization_id')

            if not organization_id or not is_valid_uuid(organization_id):
                return response_data(
                    success=False,
                    error="organization id is required",
                    status_code=400
                )

            organization = OrganizationService.get_organization_by_id(
                organization_id
            )

            if view_type == "all":
                serializer = OrganizationFullSerializer(organization)
            else:
                serializer = OrganizationMiniSerializer(organization)

            return response_data(
                success=True,
                message="Organizations fetched successfully",
                data=serializer.data,
                status_code=200
            )

        except Exception as e:
            logger.error(
                f"UserOrganizationsAPIView error user={request.user.id}: {str(e)}"
            )

            return response_data(
                success=False,
                message="Failed to fetch organizations",
                status_code=500,
                error=str(e)
            )