"""
Everything between the HTTP layer and Google: validation, the daily spend
backstop, and normalisation into the shapes docs/PLACES_MIGRATION.md section 4
publishes.

Three responsibilities, in the order a request meets them:

  1. **Validate** (section 4.1). A 3-character minimum is a cost rule, not a UX
     one — one and two-character queries are almost pure noise and every one of
     them is a billable Autocomplete event. The session token is required and
     must be a v4 UUID, because a caller that omits or reuses one turns a
     single billed session into one billed event per keystroke.

  2. **Budget** (section 4.4). A per-UTC-day counter per SKU in the cache, read
     BEFORE the call and incremented on every call that actually reaches
     Google. Google's console quotas are the outer backstop; this is the inner
     one, and it is the one that can answer with a friendly 503 instead of a
     dead picker. Details and the refresh job share the Details cap because
     they share the SKU.

  3. **Normalise**. The client returns Google's JSON verbatim; the response
     shapes the frontend reads are built here and nowhere else.

Logging is counts and outcomes. Never the query text, never the session token
(section 3 rule 7).
"""

import logging
import uuid
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from places.services import google_places_client as google
from places.services.google_places_client import (
    GooglePlacesNotConfigured,
    GooglePlacesNotFound,
    GooglePlacesRateLimited,
    GooglePlacesTimeout,
    GooglePlacesUnavailable,
)

logger = logging.getLogger(__name__)


# ── Validation limits (section 4.1) ──────────────────────────────────────────

MIN_QUERY_LENGTH = 3
MAX_QUERY_LENGTH = 100

MODE_CITY = "city"
MODE_PLACE = "place"
VALID_MODES = (MODE_CITY, MODE_PLACE)


# ── Usage counters (section 4.4) ─────────────────────────────────────────────

SKU_AUTOCOMPLETE = "autocomplete"
SKU_DETAILS = "details"
SKU_REFRESH = "refresh"

# Every SKU the counters track. `refresh` is written by the Stage B3 job, not
# by this module, but it is read here: it draws on the same Details cap.
USAGE_SKUS = (SKU_AUTOCOMPLETE, SKU_DETAILS, SKU_REFRESH)

# 48 h, so `places_usage` can still read yesterday's number tomorrow morning.
USAGE_TTL_SECONDS = 48 * 60 * 60

# Defaults matching section 1.2, used when the setting is absent.
DEFAULT_CAP_AUTOCOMPLETE = 2000
DEFAULT_CAP_DETAILS = 1000

# section 4.1: the city-mode filter is a SETTING, not code — small towns and
# villages come and go from `(cities)` depending on how Google classifies them,
# and widening the list must not need a deploy.
DEFAULT_CITY_PRIMARY_TYPES = "(cities)"

# The error code the frontend branches on for "search is off right now",
# whichever of the three reasons caused it (section 4.1).
SEARCH_UNAVAILABLE = "search_unavailable"


# ── Service-level failures, one per HTTP status the views answer with ────────


class PlacesServiceError(Exception):
    """Base for everything the views translate into a response."""


class PlacesValidationError(PlacesServiceError):
    """Bad request parameters. 400. Carries the offending field name."""

    def __init__(self, message, field="non_field_errors"):
        super().__init__(message)
        self.message = message
        self.field = field


class PlacesUnavailable(PlacesServiceError):
    """
    503 ``search_unavailable``. Three causes, one answer:

      * the daily cap is spent,
      * Google itself returned 429,
      * no API key is configured.

    All three mean "do not keep asking" and none of them are the caller's
    fault, so they are indistinguishable to the client on purpose.
    """


class PlacesUpstreamError(PlacesServiceError):
    """Google failed or timed out. 502 — retrying the same call may work."""


class PlacesNotFound(PlacesServiceError):
    """Google has no such place_id. 404."""


# ── Settings readers ─────────────────────────────────────────────────────────


def _daily_cap(sku):
    if sku == SKU_AUTOCOMPLETE:
        return int(
            getattr(
                settings,
                "PLACES_DAILY_CAP_AUTOCOMPLETE",
                DEFAULT_CAP_AUTOCOMPLETE,
            )
        )

    return int(
        getattr(settings, "PLACES_DAILY_CAP_DETAILS", DEFAULT_CAP_DETAILS)
    )


def city_primary_types():
    """
    ``PLACES_CITY_PRIMARY_TYPES`` parsed into the list Google wants.

    Comma-separated in the environment; ``(cities)`` (= locality +
    administrative_area_level_3) by default, which already covers towns like
    Thalassery and Kuthuparamba. Google caps this at 5 values and refuses to
    mix a collection like ``(cities)`` with individual types — that is a
    console-side constraint, so a bad value surfaces as a Google 400, not as a
    silent narrowing.
    """
    raw = getattr(settings, "PLACES_CITY_PRIMARY_TYPES", None)
    raw = (raw or DEFAULT_CITY_PRIMARY_TYPES).strip()

    return [part.strip() for part in raw.split(",") if part.strip()]


# ── Usage counters + circuit breaker (section 4.4) ───────────────────────────


def usage_key(sku, day=None):
    """``places:usage:<sku>:<YYYY-MM-DD>``, always the UTC day."""
    day = day or timezone.now().date()
    return f"places:usage:{sku}:{day.isoformat()}"


def get_usage(sku, day=None):
    """Today's (or ``day``'s) count for one SKU. 0 when the key has expired."""
    return int(cache.get(usage_key(sku, day)) or 0)


def increment_usage(sku):
    """
    Count one Google call.

    ``add`` then ``incr`` rather than get-then-set: ``incr`` is atomic in Redis
    but raises when the key is absent, and ``add`` only writes when it is. Two
    concurrent first-calls therefore cost at most one lost increment, never a
    lost counter — and a counter that runs slightly low is the right failure
    for a budget guard whose outer backstop is Google's own quota.

    Never raises. A cache blip must not fail a search that Google already
    answered.
    """
    key = usage_key(sku)

    try:
        cache.add(key, 0, USAGE_TTL_SECONDS)
        return cache.incr(key)
    except Exception as e:
        logger.error(f"PlacesService | usage increment failed | sku={sku} | {e}")
        return 0


def autocomplete_usage():
    """Events counted against the Autocomplete cap today."""
    return get_usage(SKU_AUTOCOMPLETE)


def details_usage():
    """
    Events counted against the Details cap today.

    ``details`` + ``refresh``: the refresh job calls the same Place Details
    endpoint and bills the same SKU, so a big refresh run has to be able to
    exhaust the same budget a user-facing search draws on.
    """
    return get_usage(SKU_DETAILS) + get_usage(SKU_REFRESH)


def check_budget(sku):
    """
    Raise PlacesUnavailable if this SKU's daily cap is spent.

    Called BEFORE the request goes out, which is the whole point: the cap only
    saves money if it stops the call, not if it hides the answer.
    """
    if sku == SKU_AUTOCOMPLETE:
        used, cap = autocomplete_usage(), _daily_cap(SKU_AUTOCOMPLETE)
    else:
        used, cap = details_usage(), _daily_cap(SKU_DETAILS)

    if used >= cap:
        logger.warning(
            f"PlacesService | Daily cap reached | sku={sku} | "
            f"used={used} | cap={cap}"
        )
        raise PlacesUnavailable(SEARCH_UNAVAILABLE)


def usage_report(days_back=1):
    """
    ``{date: {sku: count}}`` for today and the previous ``days_back`` days.

    Backs the ``places_usage`` command; kept here so the command stays a
    printer and the key format lives in one file.
    """
    today = timezone.now().date()
    report = {}

    for offset in range(days_back + 1):
        day = today - timedelta(days=offset)
        report[day] = {sku: get_usage(sku, day) for sku in USAGE_SKUS}

    return report


def caps():
    """The two configured daily caps, for the usage command's header."""
    return {
        SKU_AUTOCOMPLETE: _daily_cap(SKU_AUTOCOMPLETE),
        SKU_DETAILS: _daily_cap(SKU_DETAILS),
    }


# ── Validation (section 4.1) ─────────────────────────────────────────────────


def validate_query(raw):
    """3–100 characters after trimming. Returns the trimmed string."""
    q = (raw or "").strip()

    if len(q) < MIN_QUERY_LENGTH:
        raise PlacesValidationError(
            f"Search needs at least {MIN_QUERY_LENGTH} characters.", "q"
        )

    if len(q) > MAX_QUERY_LENGTH:
        raise PlacesValidationError(
            f"Search cannot be longer than {MAX_QUERY_LENGTH} characters.", "q"
        )

    return q


def validate_session(raw):
    """
    A v4 UUID, returned in canonical lowercase form.

    Required, and version-checked rather than merely well-formed: the session
    token is what makes a whole search bill as one session instead of one event
    per keystroke, and the client mints it with ``crypto.randomUUID()``. A
    caller that sends something else is either broken or trying to skip the
    session, and both cost real money.

    Normalising to ``str(UUID)`` is safe for consistency — a client that sends
    the same token every time sends the same NORMALISED token every time, so
    Google still sees one session.
    """
    raw = (raw or "").strip()

    if not raw:
        raise PlacesValidationError("A search session token is required.", "session")

    try:
        parsed = uuid.UUID(raw)
    except (ValueError, AttributeError, TypeError):
        raise PlacesValidationError("Invalid search session token.", "session")

    if parsed.version != 4:
        raise PlacesValidationError("Invalid search session token.", "session")

    return str(parsed)


def validate_mode(raw):
    """``city`` or ``place`` — which picker is asking."""
    mode = (raw or "").strip().lower()

    if mode not in VALID_MODES:
        raise PlacesValidationError(
            "Search mode must be 'city' or 'place'.", "mode"
        )

    return mode


def validate_bias(raw_lat, raw_lng):
    """
    The optional 50 km bias centre. Returns ``(lat, lng)`` or ``(None, None)``.

    Both or neither: half a coordinate pair is a client bug, and silently
    dropping it would hide a picker that thinks it is biasing results and is
    not. Garbage in a supplied value is a 400 for the same reason.
    """
    lat_given = raw_lat not in (None, "")
    lng_given = raw_lng not in (None, "")

    if not lat_given and not lng_given:
        return None, None

    if lat_given != lng_given:
        raise PlacesValidationError(
            "lat and lng must be provided together.", "lat"
        )

    try:
        latitude = float(raw_lat)
        longitude = float(raw_lng)
    except (TypeError, ValueError):
        raise PlacesValidationError("lat and lng must be numbers.", "lat")

    if not (-90 <= latitude <= 90):
        raise PlacesValidationError("lat must be between -90 and 90.", "lat")

    if not (-180 <= longitude <= 180):
        raise PlacesValidationError("lng must be between -180 and 180.", "lng")

    return latitude, longitude


# ── Normalisation (sections 4.1, 4.2) ────────────────────────────────────────


def _text_of(node):
    """``{"text": "..."}`` -> ``"..."``. Google omits the node when empty."""
    if not isinstance(node, dict):
        return ""

    return node.get("text") or ""


def normalise_predictions(payload):
    """
    Google's ``suggestions`` -> the ``results`` list of section 4.1.

    ``queryPrediction`` suggestions are dropped: they are free-text search
    suggestions with no place_id, so there is nothing to select, store or fetch
    details for. A prediction without a placeId is dropped for the same reason.
    """
    results = []

    for suggestion in payload.get("suggestions") or []:
        prediction = (suggestion or {}).get("placePrediction")

        if not prediction:
            continue

        place_id = prediction.get("placeId")

        if not place_id:
            continue

        structured = prediction.get("structuredFormat") or {}

        results.append(
            {
                "place_id": place_id,
                "name": _text_of(structured.get("mainText")),
                "label": _text_of(prediction.get("text")),
                "secondary": _text_of(structured.get("secondaryText")),
                "types": prediction.get("types") or [],
            }
        )

    return results


# section 4.2, in priority order: the first component carrying one of these
# types wins. Google labels the same administrative level differently from
# country to country, so a single type would leave `city` empty for most of
# the world.
CITY_COMPONENT_TYPES = (
    "locality",
    "administrative_area_level_3",
    "administrative_area_level_2",
    "sublocality_level_1",
)


def _component_by_types(components, wanted_types, key="longText"):
    """First component matching ``wanted_types``, in the order given."""
    for wanted in wanted_types:
        for component in components:
            if wanted in (component.get("types") or []):
                return component.get(key) or ""

    return ""


def normalise_details(place_id, payload):
    """
    Google's Place Details -> the object of section 4.2.

    Coordinates are Optional on the way out even though Details always returns
    them for a real place: a partial answer should degrade to a location with a
    label and no coordinates, which the model (Stage B2) accepts, rather than
    to a 502 that loses the selection the user just made.
    """
    components = payload.get("addressComponents") or []
    location = payload.get("location") or {}

    return {
        "place_id": place_id,
        "latitude": location.get("latitude"),
        "longitude": location.get("longitude"),
        "city": _component_by_types(components, CITY_COMPONENT_TYPES),
        "state": _component_by_types(
            components, ("administrative_area_level_1",)
        ),
        "country": _component_by_types(components, ("country",)),
        "country_code": _component_by_types(
            components, ("country",), key="shortText"
        ).upper(),
        "types": payload.get("types") or [],
    }


# ── The two operations the views call ────────────────────────────────────────


def _translate(error, tag):
    """
    Client exception -> service exception.

    Google's own 429 becomes the SAME 503 as our daily cap: both mean the
    quota is gone for now, and the picker shows one message for both.
    """
    if isinstance(error, GooglePlacesNotConfigured):
        return PlacesUnavailable(SEARCH_UNAVAILABLE)

    if isinstance(error, GooglePlacesRateLimited):
        logger.warning(f"{tag} | Google rate limited | serving 503")
        return PlacesUnavailable(SEARCH_UNAVAILABLE)

    if isinstance(error, GooglePlacesNotFound):
        return PlacesNotFound("Place not found")

    if isinstance(error, GooglePlacesTimeout):
        return PlacesUpstreamError("Place search timed out")

    if isinstance(error, GooglePlacesUnavailable):
        return PlacesUpstreamError("Place search failed")

    return PlacesUpstreamError("Place search failed")


def autocomplete(*, q, session, mode, lat=None, lng=None):
    """
    Validated, budgeted, normalised Autocomplete.

    Returns ``{"results": [...]}`` exactly as section 4.1 specifies.
    """
    TAG = "PlacesService.autocomplete"

    query = validate_query(q)
    session_token = validate_session(session)
    search_mode = validate_mode(mode)
    latitude, longitude = validate_bias(lat, lng)

    check_budget(SKU_AUTOCOMPLETE)

    included = city_primary_types() if search_mode == MODE_CITY else None

    try:
        payload = google.autocomplete(
            input_text=query,
            session_token=session_token,
            included_primary_types=included,
            latitude=latitude,
            longitude=longitude,
        )
    except GooglePlacesNotConfigured as e:
        raise _translate(e, TAG)
    except Exception as e:
        # Counted even though it failed: the request left the building, and a
        # failure that is not counted is a failure a retry loop can repeat for
        # free until the console quota — not ours — stops it.
        used = increment_usage(SKU_AUTOCOMPLETE)
        logger.warning(f"{TAG} | Failed | mode={search_mode} | used={used}")
        raise _translate(e, TAG)

    used = increment_usage(SKU_AUTOCOMPLETE)
    results = normalise_predictions(payload)

    # Counts and outcomes only — no query text, no session token.
    logger.info(
        f"{TAG} | OK | mode={search_mode} | biased={latitude is not None} | "
        f"results={len(results)} | used={used}/{_daily_cap(SKU_AUTOCOMPLETE)}"
    )

    return {"results": results}


def details(*, place_id, session):
    """
    Validated, budgeted, normalised Place Details for ONE selected prediction.

    Returns the object of section 4.2.
    """
    TAG = "PlacesService.details"

    place_id = (place_id or "").strip()

    if not place_id:
        raise PlacesValidationError("A place id is required.", "place_id")

    session_token = validate_session(session)

    check_budget(SKU_DETAILS)

    try:
        payload = google.place_details(
            place_id=place_id,
            session_token=session_token,
        )
    except GooglePlacesNotConfigured as e:
        raise _translate(e, TAG)
    except Exception as e:
        used = increment_usage(SKU_DETAILS)
        logger.warning(f"{TAG} | Failed | used={used}")
        raise _translate(e, TAG)

    used = increment_usage(SKU_DETAILS)
    result = normalise_details(place_id, payload)

    logger.info(
        f"{TAG} | OK | coords={result['latitude'] is not None} | "
        f"used={used}/{_daily_cap(SKU_DETAILS)}"
    )

    return result
