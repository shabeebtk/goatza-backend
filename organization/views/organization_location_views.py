import logging
from django.db import transaction
from rest_framework.permissions import IsAuthenticated
from rest_framework import serializers
from core.views.base_views import BaseAPIView
from organization.models import Organization, OrganizationLocation
from organization.services.organization_member_service import OrganizationMemberService
from utils.response import response_data
from organization.serializers.organization_location_serializers import UpsertOrganizationLocationSerializer

logger = logging.getLogger(__name__)



class OrganizationLocationAPIView(BaseAPIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        TAG = "[ORG LOCATION UPSERT]"
        
        try:
            actor = request.actor
            user = request.user

            if not actor.is_org:
                return response_data(
                    success=False,
                    error="organization request only",
                    status_code=400
                )

            org_id = actor.organization.id
            logger.info(f"{TAG} User={user.id} updating location for Org={org_id}")

            org = actor.organization
          
        except Organization.DoesNotExist:
            return response_data(success=False, message="Organization not found", status_code=404)

        try:
            serializer = UpsertOrganizationLocationSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            data = serializer.validated_data

            location_id = data.get("id")
            is_primary = data.get("is_primary", False)

            with transaction.atomic():
                # Enforce unique primary location constraint if setting this one to primary
                if is_primary:
                    OrganizationLocation.objects.filter(organization=org, is_primary=True).update(is_primary=False)

                # Update if ID is provided
                if location_id:
                    try:
                        loc = OrganizationLocation.objects.get(id=location_id, organization=org)
                        loc.name = data.get("name", loc.name)
                        loc.address = data.get("address", loc.address)
                        loc.city = data.get("city", loc.city)
                        loc.state = data.get("state", loc.state)
                        loc.country_code = data.get("country_code", loc.country_code)
                        if "latitude" in data: loc.latitude = data["latitude"]
                        if "longitude" in data: loc.longitude = data["longitude"]
                        loc.is_primary = is_primary
                        loc.save()
                        message = "Location updated successfully"
                    except OrganizationLocation.DoesNotExist:
                        return response_data(success=False, message="Location not found", status_code=404)
                # Create if no ID
                else:
                    # If this is the very first location, make it primary automatically
                    if not OrganizationLocation.objects.filter(organization=org).exists():
                        is_primary = True
                        
                    loc = OrganizationLocation.objects.create(
                        organization=org,
                        name=data.get("name", ""),
                        address=data.get("address", ""),
                        city=data.get("city"),
                        state=data.get("state", ""),
                        country_code=data.get("country_code"),
                        latitude=data.get("latitude"),
                        longitude=data.get("longitude"),
                        is_primary=is_primary
                    )
                    message = "Location added successfully"

            # Return serialized location data
            from organization.serializers.organization_serializers import OrganizationLocationSerializer
            return response_data(
                success=True,
                message=message,
                data=OrganizationLocationSerializer(loc).data
            )

        except serializers.ValidationError as e:
            return response_data(success=False, message="Validation failed", data=e.detail, status_code=400)
        except Exception as e:
            logger.exception(f"{TAG} Unexpected error org={org_id}")
            return response_data(success=False, message="Failed to upsert location", error=str(e), status_code=500)


class DeleteOrganizationLocationAPIView(BaseAPIView):
    permission_classes = [IsAuthenticated]

    def delete(self, request):
        TAG = "[ORG LOCATION DELETE]"
        try:
            actor = request.actor            
            location_id = request.query_params.get('location_id')

            if not location_id:
                return response_data(success=False, error="location_id is required", status_code=400)

            if not actor.is_org:
                return response_data(success=False, error="organization request only or provide org_id", status_code=400)

            org = actor.organization
        
            with transaction.atomic():
                loc = OrganizationLocation.objects.select_for_update().get(id=location_id, organization=org)
                was_primary = loc.is_primary
                loc.delete()

                # If we deleted the primary location, try to assign a new one
                if was_primary:
                    next_loc = OrganizationLocation.objects.filter(organization=org).first()
                    if next_loc:
                        next_loc.is_primary = True
                        next_loc.save()

            return response_data(success=True, message="Location deleted successfully")
        except OrganizationLocation.DoesNotExist:
            return response_data(success=False, message="Location not found", status_code=404)
        except Exception as e:
            logger.exception(f"{TAG} Unexpected error")
            return response_data(success=False, message="Failed to delete location", error=str(e), status_code=500)
