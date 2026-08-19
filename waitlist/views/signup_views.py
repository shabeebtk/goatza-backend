"""
The waitlist's HTTP surface, mounted under /public/ from core.public_urls.

Every view here extends ``PublicAPIView``: nobody has an account yet, so
anonymous is not an edge case on this app, it is the only case.

Two departures from the rest of the public surface, both deliberate:

  * One of these is a WRITE. Everything else under /public/ reads. The create
    view therefore overrides ``throttle_classes`` — PublicAPIView's default is
    a read budget (see waitlist.throttles.WaitlistSignupThrottle).

  * A duplicate is a success, not a 400. Somebody who cannot remember whether
    they signed up has no account to check, so re-submitting the form is the
    supported way to find out. The service returns the existing row and the
    response says which number they already are.

Thin as usual: validate, hand to PlayerSignupService or a selector, shape the
answer.
"""

import logging

from django.conf import settings
from rest_framework.exceptions import ValidationError

from core.views.base_views import PublicAPIView
from utils.errors import flatten_validation_error
from utils.response import response_data
from waitlist.selectors.signup_selectors import (
    display_count,
    display_number,
    get_by_ref_code,
    is_founding,
)
from waitlist.serializers.signup_serializers import (
    PlayerSignupCardSerializer,
    PlayerSignupCreateSerializer,
)
from waitlist.services.signup_services import PlayerSignupService
from waitlist.throttles import WaitlistSignupThrottle

logger = logging.getLogger(__name__)

# The number the landing page counts towards. A setting rather than a constant
# because it is a marketing target that moves once it is hit, and moving it
# should not be a code change.
DEFAULT_WAITLIST_GOAL = 1000

# ``source`` is CharField(50). The query string is attacker-controlled, so it
# is cut here rather than trusted to be short.
MAX_SOURCE_LENGTH = 50


def _signup_payload(signup):
    """
    The success shape — and the shape decoy_payload has to imitate.

    ``signup_number`` is the DISPLAY number. The raw column is not in this
    payload, is not in the card serializer, and is not in any other response:
    it exists in the admin and in the notification mail, and nowhere a client
    can read it.
    """
    return {
        "signup_number": display_number(signup.signup_number),
        "ref_code": signup.ref_code,
        "name": signup.name,
        "city": signup.city,
        "is_founding": is_founding(signup.signup_number),
    }


class PlayerSignupCreateAPIView(PublicAPIView):
    """
    POST /public/waitlist/players[?src=<tag>]

    Joins the waitlist. 201 for a new player, 200 for one who was already on
    the list — same body either way, plus ``already_registered`` so the client
    can pick the screen without parsing the message.
    """

    # PublicAPIView sets this to PublicReadThrottle (60/min). This endpoint
    # writes, so it gets its own far tighter bucket instead.
    throttle_classes = [WaitlistSignupThrottle]

    def post(self, request):
        TAG = "PlayerSignupCreateAPIView"

        try:
            serializer = PlayerSignupCreateSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)

            data = serializer.validated_data

            # The honeypot. Answer exactly as if it had worked — same status,
            # same keys, a plausible number — and write nothing.
            if data.pop("website", "").strip():
                logger.info(f"{TAG} | Honeypot tripped | nothing persisted")
                return response_data(
                    success=True,
                    message="You're in!",
                    status_code=201,
                    data=PlayerSignupService.decoy_payload(data),
                )

            # Attribution rides on the link ("...?src=ig_reel_04").
            #
            # The query string is the authority: it is what the Instagram bio
            # link actually carries, and a body field alone could be set to any
            # campaign by anyone. The body is read only as a FALLBACK, for the
            # client that sends `source` alongside the form (the /join page
            # sends both) or a proxy that drops query strings from a POST —
            # losing the tag silently is the failure worth avoiding here, since
            # nothing downstream can reconstruct it.
            source = str(
                request.query_params.get("src")
                or request.data.get("source")
                or ""
            ).strip()

            signup, created = PlayerSignupService.create(
                source=source[:MAX_SOURCE_LENGTH],
                **data,
            )

            if not created:
                return response_data(
                    success=True,
                    message=(
                        f"You're already on the list — you're "
                        f"#{display_number(signup.signup_number)}."
                    ),
                    status_code=200,
                    data={**_signup_payload(signup), "already_registered": True},
                )

            logger.info(
                f"{TAG} | Signup created | "
                f"signup_number={signup.signup_number} | source={source or '-'}"
            )

            return response_data(
                success=True,
                message=(
                    f"You're in — you're "
                    f"#{display_number(signup.signup_number)}."
                ),
                status_code=201,
                data={**_signup_payload(signup), "already_registered": False},
            )

        except ValidationError as e:
            flat = flatten_validation_error(e.detail)
            logger.warning(f"{TAG} | Validation Error | {flat['message']}")
            return response_data(
                success=False,
                message=flat["message"],
                status_code=400,
                error=flat["message"],
                data={"errors": flat["errors"]},
            )

        except Exception as e:
            logger.error(f"{TAG} | Error | {str(e)}")
            return response_data(
                success=False,
                message="Something went wrong",
                status_code=500,
                error=str(e),
            )


class WaitlistStatsAPIView(PublicAPIView):
    """
    GET /public/waitlist/stats

    ``{"count": 412, "goal": 1000}`` — the progress bar on the landing page.
    Count is the DISPLAY count (real signups plus WAITLIST_DISPLAY_OFFSET),
    cached for a minute by the selector; goal is a setting.
    """

    def get(self, request):
        TAG = "WaitlistStatsAPIView"

        try:
            return response_data(
                success=True,
                data={
                    "count": display_count(),
                    "goal": getattr(
                        settings,
                        "WAITLIST_GOAL",
                        DEFAULT_WAITLIST_GOAL,
                    ),
                },
            )

        except Exception as e:
            logger.error(f"{TAG} | Error | {str(e)}")
            return response_data(
                success=False,
                message="Something went wrong",
                status_code=500,
                error=str(e),
            )


class PlayerSignupCardAPIView(PublicAPIView):
    """
    GET /public/waitlist/players/<ref_code>

    The shareable card, and nothing else: name, display number, city, country,
    position, sport and whether they are a founding player. Phone, email,
    Instagram and the coordinates are NOT in the serializer — a ref code is a
    short public string that appears in screenshots, so anything reachable by
    guessing one is, in effect, published.
    """

    def get(self, request, ref_code):
        TAG = "PlayerSignupCardAPIView"

        try:
            signup = get_by_ref_code(ref_code)

            if signup is None:
                return response_data(
                    success=False,
                    message="Signup not found",
                    status_code=404,
                )

            return response_data(
                success=True,
                data=PlayerSignupCardSerializer(signup).data,
            )

        except Exception as e:
            logger.error(f"{TAG} | Error | ref_code={ref_code} | {str(e)}")
            return response_data(
                success=False,
                message="Something went wrong",
                status_code=500,
                error=str(e),
            )
