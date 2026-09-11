"""
The ONLY code that talks to Google. Nothing else in the backend imports
``requests`` for Places.

Deliberately thin: build the request, send it, turn a failure into one of the
typed exceptions below, hand the parsed JSON back untouched. Validation,
budgeting and normalisation are places_service's job — keeping them out of here
is what makes "did we call Google correctly?" answerable by reading one file.

Two rules this module exists to enforce (docs/PLACES_MIGRATION.md section 3):

  * **The field masks are constants, not parameters callers compose.** Every
    field outside them costs money — ``displayName`` moves Place Details from
    the Essentials SKU to Pro, and photos/rating/hours/website move it to
    Enterprise. The place NAME comes from the autocomplete prediction, never
    from Details. Adding a field here is a pricing decision, not a code change.

  * **Nothing is logged that identifies what was searched.** No input text, no
    session token, no place_id. Status codes and outcomes only.

No retries: the caller is a user typing, and a second 4-second wait after a
timeout is worse than an error. No caching either — section 3 rule 8 forbids
caching Google responses, and the session token only prices correctly if every
request in a session actually reaches Google.
"""

import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)


AUTOCOMPLETE_URL = "https://places.googleapis.com/v1/places:autocomplete"
DETAILS_URL = "https://places.googleapis.com/v1/places/{place_id}"

# 4 seconds, connect + read. A city picker that has not answered in four
# seconds has already lost the user; failing fast frees the worker.
TIMEOUT_SECONDS = 4

# ── Field masks (docs/PLACES_MIGRATION.md section 4) ─────────────────────────

# Autocomplete: place id, the full one-line text, the main/secondary split and
# the types. `structuredFormat` is what gives us `name` and `secondary` without
# a Details call.
AUTOCOMPLETE_FIELD_MASK = (
    "suggestions.placePrediction.placeId,"
    "suggestions.placePrediction.text,"
    "suggestions.placePrediction.structuredFormat,"
    "suggestions.placePrediction.types"
)

# Place Details, user-facing selection. Essentials SKU — exactly these three.
DETAILS_FIELD_MASK = "location,addressComponents,types"

# Place Details, coordinate refresh job (section 4.2 last line, used from
# Stage B3). Coordinates only: the row already has its label, and the refresh
# is a background call nobody is waiting on.
DETAILS_REFRESH_FIELD_MASK = "location"

LANGUAGE_CODE = "en"

# section 4.1: bias, not restriction — a 50 km circle around the actor's own
# coordinates promotes nearby matches without hiding distant ones.
LOCATION_BIAS_RADIUS_METRES = 50000.0


# ── Typed failures ───────────────────────────────────────────────────────────


class GooglePlacesError(Exception):
    """Base for every failure this client raises."""


class GooglePlacesNotConfigured(GooglePlacesError):
    """GOOGLE_PLACES_API_KEY is unset. The app boots; search does not work."""


class GooglePlacesTimeout(GooglePlacesError):
    """Google did not answer inside TIMEOUT_SECONDS."""


class GooglePlacesRateLimited(GooglePlacesError):
    """
    Google returned 429 — the console quota cap, our outer backstop.

    Distinct from every other error because the caller's response differs: this
    is "stop asking today", not "that request failed".
    """


class GooglePlacesNotFound(GooglePlacesError):
    """Google returned 404 for a place_id (stale or invalid)."""


class GooglePlacesUnavailable(GooglePlacesError):
    """Any other Google failure: 4xx, 5xx, connection error, unparseable body."""


def _api_key():
    key = getattr(settings, "GOOGLE_PLACES_API_KEY", None)

    if not key:
        # A WARNING here rather than an exception at import time: a missing key
        # must not stop the app booting, since the rest of the API has nothing
        # to do with Places. The endpoints answer 503 and this line says why.
        logger.warning(
            "GooglePlacesClient | GOOGLE_PLACES_API_KEY is not set | "
            "place search disabled"
        )
        raise GooglePlacesNotConfigured("GOOGLE_PLACES_API_KEY is not set")

    return key


def _send(method, url, *, headers, params=None, json_body=None, tag):
    """
    One request, one typed outcome.

    Every Google call in the app funnels through here so the timeout, the error
    mapping and the (deliberately anonymous) log line are written once.
    """
    try:
        response = requests.request(
            method,
            url,
            headers=headers,
            params=params,
            json=json_body,
            timeout=TIMEOUT_SECONDS,
        )
    except requests.Timeout:
        logger.warning(f"{tag} | Timeout after {TIMEOUT_SECONDS}s")
        raise GooglePlacesTimeout("Google Places timed out")
    except requests.RequestException as e:
        # Connection reset, DNS failure, TLS error. type(e).__name__ rather
        # than str(e): the exception text can carry the full request URL, and
        # for Details that URL contains the session token.
        logger.error(f"{tag} | Transport error | {type(e).__name__}")
        raise GooglePlacesUnavailable("Google Places request failed")

    status = response.status_code

    if status == 429:
        logger.warning(f"{tag} | Google rate limited | status=429")
        raise GooglePlacesRateLimited("Google Places rate limited")

    if status == 404:
        logger.warning(f"{tag} | Not found | status=404")
        raise GooglePlacesNotFound("Place not found")

    if status >= 400:
        # response.text can echo the request back. Status only.
        logger.error(f"{tag} | Google error | status={status}")
        raise GooglePlacesUnavailable(f"Google Places returned {status}")

    try:
        return response.json()
    except ValueError:
        logger.error(f"{tag} | Unparseable response body | status={status}")
        raise GooglePlacesUnavailable("Google Places returned a non-JSON body")


def autocomplete(
    *,
    input_text,
    session_token,
    included_primary_types=None,
    latitude=None,
    longitude=None,
):
    """
    POST /v1/places:autocomplete — one keystroke's worth of predictions.

    ``included_primary_types`` is the city-mode filter and comes from
    settings.PLACES_CITY_PRIMARY_TYPES; venue mode passes None so Google
    returns grounds, turfs, academies and stadiums alongside localities.

    ``latitude``/``longitude`` (both or neither) add the 50 km bias circle.

    Returns Google's raw JSON. Raises GooglePlaces*.
    """
    TAG = "GooglePlacesClient.autocomplete"

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": _api_key(),
        "X-Goog-FieldMask": AUTOCOMPLETE_FIELD_MASK,
    }

    body = {
        "input": input_text,
        "sessionToken": session_token,
        "languageCode": LANGUAGE_CODE,
    }

    if included_primary_types:
        body["includedPrimaryTypes"] = list(included_primary_types)

    if latitude is not None and longitude is not None:
        body["locationBias"] = {
            "circle": {
                "center": {"latitude": latitude, "longitude": longitude},
                "radius": LOCATION_BIAS_RADIUS_METRES,
            }
        }

    return _send(
        "POST",
        AUTOCOMPLETE_URL,
        headers=headers,
        json_body=body,
        tag=TAG,
    )


def place_details(*, place_id, session_token=None, field_mask=None):
    """
    GET /v1/places/<place_id> — called for the ONE prediction the user picked,
    never for the list (section 3 rule 4).

    ``session_token`` closes the autocomplete session it belongs to, which is
    what makes the whole search bill as a single session. The refresh job
    passes None (there is no session to close) together with
    ``field_mask=DETAILS_REFRESH_FIELD_MASK``.

    Returns Google's raw JSON. Raises GooglePlaces*, including
    GooglePlacesNotFound when the place_id no longer resolves.
    """
    TAG = "GooglePlacesClient.place_details"

    headers = {
        "X-Goog-Api-Key": _api_key(),
        "X-Goog-FieldMask": field_mask or DETAILS_FIELD_MASK,
    }

    params = {"languageCode": LANGUAGE_CODE}

    if session_token:
        params["sessionToken"] = session_token

    return _send(
        "GET",
        DETAILS_URL.format(place_id=place_id),
        headers=headers,
        params=params,
        tag=TAG,
    )
