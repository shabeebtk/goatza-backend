"""
The proxy's HTTP surface, mounted at ``places/`` from core.urls.

Thin, like every other view in the repo: pull the query params, hand them to
PlacesService, translate its typed exception into a status code. No validation,
no Google call and no shaping happens here.

**AllowAny, and NOT under /public/.** The public `/join` form uses the city
picker before anybody has an account, so authentication cannot be required. It
still sits on its own prefix rather than in core.public_urls, because that file
is an allow-list of anonymous reads of OUR data — these two endpoints read
nothing of ours; they spend money at Google. The protection that matters here
is the throttles plus the daily cap, not a permission class.

``APIView`` rather than ``core.views.base_views.PublicAPIView`` for the same
reason: PublicAPIView drags in ActorMixin, whose actor resolution costs a query
per request to verify org membership, and nothing here has any use for the
actor. Autocomplete fires on every keystroke past the third.

Status codes (section 4.1):
  400 bad params · 404 unknown place_id · 429 throttled ·
  503 ``search_unavailable`` (daily cap, Google 429, or no API key) ·
  502 Google failed or timed out.
"""

import logging

from rest_framework.permissions import AllowAny
from rest_framework.views import APIView

from places.services.places_service import (
    PlacesNotFound,
    PlacesUnavailable,
    PlacesUpstreamError,
    PlacesValidationError,
    autocomplete as autocomplete_places,
    details as place_details,
)
from places.throttles import (
    AutocompleteAnonThrottle,
    AutocompleteUserThrottle,
    DetailsAnonThrottle,
    DetailsUserThrottle,
)
from utils.errors import error_body
from utils.response import response_data

logger = logging.getLogger(__name__)


class PlacesAPIView(APIView):
    """
    Shared permissions and the one error translation both endpoints need.

    Every branch answers through ``utils.response.response_data`` so the
    envelope matches the rest of the API; the shapes section 4 publishes are
    what lands in ``data``.
    """

    permission_classes = [AllowAny]

    def _error(self, error, tag):
        if isinstance(error, PlacesValidationError):
            logger.warning(f"{tag} | Bad request | field={error.field}")
            return response_data(
                success=False,
                message=error.message,
                status_code=400,
                error=error.message,
                data=error_body(error.message, error.field),
            )

        if isinstance(error, PlacesUnavailable):
            # The literal code section 4.1 names, both at the top level and
            # inside the envelope, so a client can branch on either.
            return response_data(
                success=False,
                message="Place search is unavailable right now.",
                status_code=503,
                error="search_unavailable",
                data=error_body("search_unavailable"),
            )

        if isinstance(error, PlacesNotFound):
            return response_data(
                success=False,
                message="Place not found",
                status_code=404,
            )

        if isinstance(error, PlacesUpstreamError):
            return response_data(
                success=False,
                message="Place search failed. Please try again.",
                status_code=502,
                error=str(error),
            )

        logger.error(f"{tag} | Error | {type(error).__name__}")
        return response_data(
            success=False,
            message="Something went wrong",
            status_code=500,
            error=str(error),
        )


class PlacesAutocompleteAPIView(PlacesAPIView):
    """
    GET places/autocomplete?q=<3-100 chars>&session=<uuid v4>&mode=city|place
        [&lat=<float>&lng=<float>]

    ``{"results": [{place_id, name, label, secondary, types}, ...]}``.

    ``session`` is the same UUID for every keystroke of one search AND for the
    details call that closes it — that is what makes the search bill as one
    session instead of one event per request.
    """

    throttle_classes = [AutocompleteUserThrottle, AutocompleteAnonThrottle]

    def get(self, request):
        TAG = "PlacesAutocompleteAPIView"

        try:
            params = request.query_params

            return response_data(
                success=True,
                data=autocomplete_places(
                    q=params.get("q"),
                    session=params.get("session"),
                    mode=params.get("mode"),
                    lat=params.get("lat"),
                    lng=params.get("lng"),
                ),
            )

        except Exception as e:
            return self._error(e, TAG)


class PlaceDetailsAPIView(PlacesAPIView):
    """
    GET places/details/<place_id>?session=<uuid v4>

    ``{place_id, latitude, longitude, city, state, country, country_code,
    types}``.

    Called for the ONE prediction the user selected and never for the list
    (section 3 rule 4) — the frontend is what enforces that, and the Details
    throttle above is what makes a client that forgets fail loudly.
    """

    throttle_classes = [DetailsUserThrottle, DetailsAnonThrottle]

    def get(self, request, place_id):
        TAG = "PlaceDetailsAPIView"

        try:
            return response_data(
                success=True,
                data=place_details(
                    place_id=place_id,
                    session=request.query_params.get("session"),
                ),
            )

        except Exception as e:
            return self._error(e, TAG)
