# services/geo/haversine.py
"""
The one copy of the "how far is this row from that point" trig.

Explore (players / organizations) and recruitment discovery both rank by
distance against a different table. Two copies of this would be two chances to
drop the acos clamp — a bug that only shows up when a row sits (almost) exactly
on the viewer, which no fixture ever does. So the trig lives here and callers
import it; ``ExploreService._distance_expr`` / ``_bounding_box`` stay as thin
delegations so their existing call sites read unchanged.
"""

import math

from django.db.models import (
    Case, ExpressionWrapper, F, FloatField, Q, Value, When,
)
from django.db.models.functions import ACos, Cos, Greatest, Least, Radians, Sin

EARTH_RADIUS_KM = 6371.0

# ~km per degree of latitude (roughly constant across the globe).
KM_PER_DEG_LAT = 111.0


def bounding_box(lat, lng, radius):
    """
    Lat/lng min/max box of ``radius`` km around (lat, lng). Used as a cheap,
    index-friendly prefilter before the exact haversine distance.
    """
    lat_delta = radius / KM_PER_DEG_LAT

    # Longitude degrees shrink toward the poles; guard cos()→0 so the box
    # never blows up to an infinite width near ±90°.
    cos_lat = max(abs(math.cos(math.radians(lat))), 0.01)
    lng_delta = radius / (KM_PER_DEG_LAT * cos_lat)

    return {
        "min_lat": max(lat - lat_delta, -90.0),
        "max_lat": min(lat + lat_delta, 90.0),
        "min_lng": lng - lng_delta,
        "max_lng": lng + lng_delta,
    }


def distance_between(lat1, lng1, lat2, lng2):
    """
    Kilometres between two points, in PYTHON — or None when any coordinate is
    missing.

    FOR ROWS ALREADY LOADED, and only those: a handful of trial centres hanging
    off one recruitment, where a round trip to the database would cost more
    than the trig does. ``distance_expr`` remains the only way to measure
    INSIDE a query — anything that filters, sorts or paginates on distance has
    to do it in SQL, because a Python sort can only ever reorder the page it
    was handed.

    None means UNKNOWN and never 0: a centre whose coordinates nobody has
    resolved is not one the viewer is standing on, and a 0 would sort it to
    the top of a nearest-first list.

    The acos input is clamped to [-1, 1] for the same reason the expression
    clamps it — float rounding at distance ≈ 0 otherwise trips a domain error,
    which is exactly the case a viewer at the venue produces.
    """
    if lat1 is None or lng1 is None or lat2 is None or lng2 is None:
        return None

    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)

    cos_angle = (
        math.sin(phi1) * math.sin(phi2)
        + math.cos(phi1) * math.cos(phi2)
        * math.cos(math.radians(lng2) - math.radians(lng1))
    )

    return math.acos(max(-1.0, min(1.0, cos_angle))) * EARTH_RADIUS_KM


def distance_expr(lat, lng, lat_field, lng_field):
    """
    Haversine distance (km) from a fixed (lat, lng) to each row's
    (lat_field, lng_field) as an ORM expression:

        R * acos( sin(lat1)sin(lat2) + cos(lat1)cos(lat2)cos(lng2 - lng1) )

    The acos input is clamped to [-1, 1] (Least/Greatest) so float rounding
    at distance ≈ 0 never trips an acos domain error.

    A row with a NULL coordinate annotates to NULL — "unknown distance", which
    callers must never read as "right here". The explicit isnull guard is what
    makes that true: Postgres LEAST/GREATEST SKIP nulls, so the clamp above
    would quietly turn a NULL cos into -1 and report the row as 20 015 km away,
    on the exact opposite side of the planet. Explore never noticed because its
    bounding box drops null coordinates before the trig runs; recruitment
    discovery annotates the whole candidate set, so it did.
    """
    lat1 = math.radians(lat)
    lng1 = math.radians(lng)
    sin_lat1 = math.sin(lat1)
    cos_lat1 = math.cos(lat1)

    cos_angle = (
        Value(sin_lat1) * Sin(Radians(F(lat_field)))
        + Value(cos_lat1)
        * Cos(Radians(F(lat_field)))
        * Cos(Radians(F(lng_field)) - Value(lng1))
    )
    clamped = Least(Value(1.0), Greatest(Value(-1.0), cos_angle))

    return Case(
        When(
            Q(**{f"{lat_field}__isnull": True})
            | Q(**{f"{lng_field}__isnull": True}),
            then=Value(None, output_field=FloatField()),
        ),
        default=ExpressionWrapper(
            ACos(clamped) * Value(EARTH_RADIUS_KM),
            output_field=FloatField(),
        ),
        output_field=FloatField(),
    )
