"""
The place side of an ``OrganizationLocation``.

A branch row is two things wearing one payload: the ORG's own facts (branch
name, street address, which one is primary) and the PLACE it sits at. Only the
second half is shared with the rest of the app, so it is the only half that goes
through ``LocationService``:

  * ``name`` on the payload is the branch label ("Main Branch") and never
    reaches ``Location.name``. The place's own label arrives as
    ``location_name`` — the same word ``UserProfile`` and ``Post`` use for it —
    and falls back to the city when a client sends only that.
  * the denormalized columns (city / state / country_code / latitude /
    longitude) are refilled from the resolved Location when there is one, so
    every actor pointing at a place agrees on what it is called and where it is.
    Explore reads those columns directly and never joins the FK.
  * the FK is what ``LocationService.propagate_coords`` follows when the refresh
    job re-fetches or expires a place's coordinates.

See docs/PLACES_MIGRATION.md sections 5.2 and 5.4.
"""

import logging

from services.location.location_service import LocationService
from shared.models import Location

logger = logging.getLogger(__name__)

# The keys of the branch payload that describe the PLACE, in the shape
# LocationService reads (section 5.4).
PLACE_KEYS = (
    "provider",
    "external_id",
    "location_name",
    "type",
    "city",
    "state",
    "country",
    "country_code",
    "latitude",
    "longitude",
)


def build_place_payload(data: dict) -> dict:
    """The section 5.4 dict, assembled out of the flat branch payload."""
    name = (
        data.get("location_name")
        or data.get("city")
        or ""
    )

    return {
        "provider": data.get("provider") or Location.Provider.GOOGLE,
        "external_id": data.get("external_id", "") or "",
        "name": name,
        "type": data.get("type") or Location.Type.CITY,
        "city": data.get("city", "") or "",
        "state": data.get("state", "") or "",
        "country": data.get("country", "") or "",
        "country_code": data.get("country_code", "") or "",
        "latitude": data.get("latitude"),
        "longitude": data.get("longitude"),
    }


def resolve_place(data: dict):
    """
    ``(location, columns)`` for a branch payload.

    ``location`` is None whenever there is nothing to resolve or the resolve
    failed — a branch is an address somebody typed, and it has to survive a
    geocoder that cannot place it. ``columns`` is always the set of denormalized
    values to write, taken from the resolved row when there is one and from the
    payload otherwise.
    """
    payload = build_place_payload(data)

    columns = {
        "city": payload["city"],
        "state": payload["state"],
        "country_code": payload["country_code"],
        "latitude": payload["latitude"],
        "longitude": payload["longitude"],
    }

    # Nothing to look up and nothing to create: no id and no point.
    if not payload["external_id"] and not LocationService.has_coords(
        payload["latitude"], payload["longitude"]
    ):
        return None, columns

    if not payload["name"]:
        # Location.name is not nullable and a nameless row is unusable.
        return None, columns

    try:
        location = LocationService.get_or_create_location(payload)
    except ValueError as e:
        logger.warning(
            f"OrganizationLocation | Place not resolved | "
            f"name={payload['name'] or '-'} | {str(e)}"
        )
        return None, columns

    if location is None:
        logger.warning(
            f"OrganizationLocation | Place resolved to nothing | "
            f"name={payload['name'] or '-'}"
        )
        return None, columns

    denormalized = LocationService.build_denormalized(location)

    # The resolved row wins where it has something to say — it is the row that
    # gets refreshed and corrected — but it must not blank a value the client
    # sent (a venue outside a named locality carries no city of its own).
    return location, {
        "city": denormalized["city"] or columns["city"],
        "state": location.state or columns["state"],
        "country_code": (
            denormalized["country_code"] or columns["country_code"]
        ),
        "latitude": denormalized["latitude"],
        "longitude": denormalized["longitude"],
    }
