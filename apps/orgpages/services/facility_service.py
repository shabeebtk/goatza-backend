"""
Facilities, per branch: catalog items + the club's own words, and photos.
``page.manage``.

A branch is one of the org's OrganizationLocation rows; another org's branch
reads exactly like a missing one (404). Photos are uploaded as type "org_page"
and attached here; deleting one deletes its object after commit, and so does
deleting the branch (``photo_keys_for_location``).
"""

import logging

from django.db import transaction
from django.db.models import Max

from apps.organization.models import OrganizationLocation
from apps.orgpages import constants as C
from apps.orgpages.errors import PageError, invalid, not_found
from apps.orgpages.models import FacilityItem, FacilityPhoto, LocationFacilities
from apps.orgpages.services.cleaning import clean_ids, clean_photo, clean_string_list
from apps.orgpages.services.guards import (
    lock_page,
    relock_actor,
    require_page_permission,
)
from apps.orgpages.services.public_cache import bust_public_page
from shared.tasks import delete_keys
from utils.background_jobs import enqueue
from utils.validations import is_valid_uuid

logger = logging.getLogger(__name__)

LOCATION_NOT_FOUND = "This location wasn't found."
PHOTO_NOT_FOUND = "This photo wasn't found."


def delete_objects_later(keys):
    """The objects, deleted after commit and retried — never fails the write."""
    keys = [key for key in keys if key]
    if keys:
        enqueue(delete_keys, (keys,))


def photo_keys_for_location(location):
    """Every facility photo key of a branch — for the branch's own delete."""
    return list(
        FacilityPhoto.objects.filter(location=location)
        .values_list("photo_public_id", flat=True)
    )


def get_org_location(organization, location_id):
    location = (
        OrganizationLocation.objects.filter(id=location_id, organization=organization).first()
        if is_valid_uuid(str(location_id)) else None
    )
    if location is None:
        raise not_found(LOCATION_NOT_FOUND)
    return location


def _clean_items(value):
    """Catalog keys → active FacilityItem rows. An unknown key is 400 on items."""
    if value is None:
        value = []
    if not isinstance(value, list) or not all(isinstance(key, str) for key in value):
        raise invalid("Choose facilities from the list.", "items")

    keys = list(dict.fromkeys(key.strip() for key in value))
    items = list(FacilityItem.objects.filter(key__in=keys, is_active=True))
    if len(items) != len(keys):
        raise invalid("Choose facilities from the list.", "items")
    return items


class FacilityService:

    @staticmethod
    def set_location_facilities(actor, location_id, payload):
        """PUT page/facilities/<location_id> ``{items, custom_items}`` — replaces both lists."""
        organization, member = require_page_permission(actor)
        payload = payload if isinstance(payload, dict) else {}

        items = _clean_items(payload.get("items"))
        custom_items = clean_string_list(
            payload.get("custom_items"), "custom_items", label="Other facilities",
            max_items=C.MAX_CUSTOM_FACILITIES,
            min_len=C.CUSTOM_FACILITY_MIN, max_len=C.CUSTOM_FACILITY_MAX,
        )

        with transaction.atomic():
            lock_page(organization)
            relock_actor(member)

            location = get_org_location(organization, location_id)
            facilities, _ = LocationFacilities.objects.get_or_create(location=location)
            facilities.custom_items = custom_items
            facilities.save(update_fields=["custom_items", "updated_at"])
            facilities.items.set(items)
            bust_public_page(organization.id)

        return location

    @staticmethod
    def add_photo(actor, location_id, payload):
        """POST page/facilities/<location_id>/photos ``{photo_url, photo_public_id}``. Max 12."""
        organization, member = require_page_permission(actor)
        payload = payload if isinstance(payload, dict) else {}
        url, public_id = clean_photo(
            actor, organization, payload.get("photo_url"), payload.get("photo_public_id")
        )

        with transaction.atomic():
            lock_page(organization)
            relock_actor(member)

            location = get_org_location(organization, location_id)
            photos = FacilityPhoto.objects.filter(location=location)
            if photos.count() >= C.MAX_PHOTOS_PER_LOCATION:
                raise PageError(
                    "photo_limit",
                    f"A location can have up to {C.MAX_PHOTOS_PER_LOCATION} photos.",
                )
            if photos.filter(photo_public_id=public_id).exists():
                raise PageError("already_added", "This photo is already added.")

            highest = photos.aggregate(highest=Max("order"))["highest"]
            photo = FacilityPhoto.objects.create(
                location=location,
                photo_url=url,
                photo_public_id=public_id,
                order=0 if highest is None else highest + 1,
            )
            bust_public_page(organization.id)

        return photo

    @staticmethod
    def delete_photo(actor, photo_id):
        """POST page/facilities/photos/<photo_id>/delete — the object goes after commit."""
        organization, member = require_page_permission(actor)

        with transaction.atomic():
            lock_page(organization)
            relock_actor(member)

            photo = (
                FacilityPhoto.objects
                .filter(id=photo_id, location__organization=organization)
                .first()
                if is_valid_uuid(str(photo_id)) else None
            )
            if photo is None:
                raise not_found(PHOTO_NOT_FOUND)

            public_id = photo.photo_public_id
            photo.delete()
            delete_objects_later([public_id])
            bust_public_page(organization.id)

    @staticmethod
    def reorder_photos(actor, location_id, payload):
        """``{ids}`` must be exactly that branch's photos."""
        organization, member = require_page_permission(actor)

        with transaction.atomic():
            lock_page(organization)
            relock_actor(member)

            location = get_org_location(organization, location_id)
            photos = {
                str(photo.id): photo
                for photo in FacilityPhoto.objects.filter(location=location)
            }
            ids = clean_ids(payload, photos)

            changed = []
            for index, photo_id in enumerate(ids):
                photo = photos[photo_id]
                if photo.order != index:
                    photo.order = index
                    changed.append(photo)
            if changed:
                FacilityPhoto.objects.bulk_update(changed, ["order"])
                bust_public_page(organization.id)

        return location
