"""Facilities per branch, as the club edits them. ``page.manage``."""

from apps.orgpages.selectors.editor_selectors import editor_facilities
from apps.orgpages.serializers.page_serializers import (
    catalog_payload,
    facility_photo_payload,
    location_mini,
)
from apps.orgpages.services.facility_service import FacilityService
from apps.orgpages.services.guards import require_page_permission
from apps.orgpages.views.base import PageBaseView, body_of
from utils.response import response_data


def _branch_payload(branch):
    return {
        "location": location_mini(branch["location"]),
        "items": branch["item_keys"],
        "custom_items": branch["custom_items"],
        "photos": [facility_photo_payload(photo) for photo in branch["photos"]],
    }


def facilities_payload(organization):
    catalog, branches = editor_facilities(organization)
    return {
        "catalog": catalog_payload(catalog),
        "locations": [_branch_payload(branch) for branch in branches],
    }


def branch_response(organization, location_id, *, message="", status_code=200):
    _, branches = editor_facilities(organization)
    branch = next(
        (b for b in branches if str(b["location"].id) == str(location_id).lower()), None
    )
    return response_data(
        success=True,
        message=message,
        data=_branch_payload(branch) if branch else {},
        status_code=status_code,
    )


class PageFacilitiesAPIView(PageBaseView):
    """GET /page/facilities → {catalog, locations}"""

    def get(self, request):
        organization, _ = require_page_permission(request.actor)
        return response_data(success=True, data=facilities_payload(organization))


class PageLocationFacilitiesAPIView(PageBaseView):
    """PUT /page/facilities/<location_id> {items, custom_items} → that branch"""

    def put(self, request, location_id):
        FacilityService.set_location_facilities(request.actor, location_id, body_of(request))
        return branch_response(
            request.actor.organization, location_id, message="Facilities saved"
        )


class PageFacilityPhotosAPIView(PageBaseView):
    """POST /page/facilities/<location_id>/photos {photo_url, photo_public_id} → 201 that branch"""

    def post(self, request, location_id):
        FacilityService.add_photo(request.actor, location_id, body_of(request))
        return branch_response(
            request.actor.organization, location_id,
            message="Photo added", status_code=201,
        )


class PageFacilityPhotoDeleteAPIView(PageBaseView):
    """POST /page/facilities/photos/<photo_id>/delete"""

    def post(self, request, photo_id):
        FacilityService.delete_photo(request.actor, photo_id)
        return response_data(success=True, message="Photo deleted")


class PageFacilityPhotosReorderAPIView(PageBaseView):
    """POST /page/facilities/<location_id>/photos/reorder {ids} → that branch"""

    def post(self, request, location_id):
        FacilityService.reorder_photos(request.actor, location_id, body_of(request))
        return branch_response(
            request.actor.organization, location_id, message="Order saved"
        )
