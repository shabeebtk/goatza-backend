import logging
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView
from rest_framework import serializers
from core.views.base_views import BaseAPIView
from organization.models import Organization
from organization.services.organization_service import OrganizationService
from organization.serializers.organization_serializers import (
    OrganizationCreateSerializer, OrganizationMiniSerializer, OrganizationFullSerializer, 
    UpdateOrganizationMediaSerializer
)
from utils.response import response_data
from utils.validations import is_valid_uuid
from services.storage.factory import get_storage_service
from services.storage.validators import validate_media, DEFAULT_IMAGE_EXTENSIONS


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
        




class UpdateOrganizationMediaAPIView(BaseAPIView):
    """
    upload:
    {
        "logo": "https://res.cloudinary.com/....webp",
        "logo_public_id": "users/{user_id}/organizations/{org_id}/organization_logo/logo",

        "cover_image": "https://res.cloudinary.com/....webp",
        "cover_image_public_id": "users/{user_id}/organizations/{org_id}/organization_cover/cover"
    }

    delete:
    {
        "is_delete_logo": true,
        "is_delete_cover": true
    }
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        try:
            actor = request.actor
            if not actor.is_org:
                return response_data(
                    success=False,
                    error="organization request only",
                    status_code=400
                )
        
            org_id = actor.organization.id

            serializer = UpdateOrganizationMediaSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            data = serializer.validated_data

            try:
                org = Organization.objects.select_related("profile").get(id=org_id)
            except Organization.DoesNotExist:
                return response_data(
                    success=False,
                    message="Organization not found",
                    status_code=404
                )

            profile = org.profile
            storage = get_storage_service()

            update_fields = []

            # DELETE LOGO
            if data.get("is_delete_logo"):
                if profile.logo_public_id:
                    storage.delete_file(profile.logo_public_id)

                profile.logo = ""
                profile.logo_public_id = ""

                update_fields += ["logo", "logo_public_id"]

            # DELETE COVER
            if data.get("is_delete_cover"):
                if profile.cover_image_public_id:
                    storage.delete_file(profile.cover_image_public_id)

                profile.cover_image = ""
                profile.cover_image_public_id = ""

                update_fields += ["cover_image", "cover_image_public_id"]

            # UPDATE LOGO
            if "logo" in data:
                validate_media(
                    request.user,
                    data["logo"],
                    data["logo_public_id"],
                    allowed_extensions=DEFAULT_IMAGE_EXTENSIONS
                )

                profile.logo = data["logo"]
                profile.logo_public_id = data["logo_public_id"]

                update_fields += ["logo", "logo_public_id"]

            # UPDATE COVER
            if "cover_image" in data:
                validate_media(
                    request.user,
                    data["cover_image"],
                    data["cover_image_public_id"],
                    allowed_extensions=DEFAULT_IMAGE_EXTENSIONS
                )

                profile.cover_image = data["cover_image"]
                profile.cover_image_public_id = data["cover_image_public_id"]

                update_fields += ["cover_image", "cover_image_public_id"]

            if update_fields:
                update_fields.append("updated_at")
                profile.save(update_fields=update_fields)

            return response_data(
                success=True,
                message="Organization media updated successfully"
            )

        except serializers.ValidationError as e:
            return response_data(
                success=False,
                message=str(e),
                status_code=400
            )

        except ValueError as e:
            return response_data(
                success=False,
                message=str(e),
                status_code=400
            )

        except Exception as e:
            return response_data(
                success=False,
                message=f"Failed to update media: {str(e)}",
                status_code=500
            )