"""
HTTP entry points for the Match Diary, mounted at /matches/.

Thin by design: resolve what the URL names, hand the actor and the validated
body to MatchService / the selectors, and shape the answer. The "players only,
acting as themselves" rule is NOT re-checked here — the service owns it and
raises PermissionDenied, which ``_service_error`` turns into the standard 403
envelope. One rule, one place.

Every endpoint is owner-scoped and none is public in v1. There is no username in
any of these URLs, so there is no path by which one player reads another's
diary, and no visibility check that could be forgotten.

Query parameters are parsed here rather than in the selectors: a selector that
had to defend itself against the string "banana" would be answering to HTTP,
and a bad ``?year=`` must be a 400 with a sentence in it, never a 500.
"""

import logging

from rest_framework.exceptions import (
    NotFound,
    PermissionDenied,
    ValidationError,
)

from core.views.base_views import BaseAPIView
from matches.models import MatchEntry
from matches.selectors.match_selectors import (
    active_stat_fields,
    list_matches,
    owned_match,
    upcoming_matches,
)
from matches.selectors.summary_selectors import (
    get_showcase_user,
    match_summary,
)
from matches.serializers.diary_settings_serializers import (
    MatchDiarySettingsSerializer,
    MatchDiarySettingsUpdateSerializer,
)
from matches.serializers.match_serializers import (
    MatchEntryCreateSerializer,
    MatchEntrySerializer,
    MatchEntryUpdateSerializer,
    UpcomingMatchSerializer,
)
from matches.serializers.match_stat_field_serializers import (
    MatchStatFieldSerializer,
)
from matches.serializers.summary_serializers import (
    MatchSummarySerializer,
    ShowcaseMatchSummarySerializer,
)
from matches.services.diary_settings_services import DiarySettingsService
from matches.services.match_services import MatchService
from sports.models import Sport
from utils.errors import error_body, flatten_validation_error
from utils.response import response_data
from utils.validations import is_valid_uuid

logger = logging.getLogger(__name__)


# A diary page. 20 is what the client renders; 50 is the ceiling so one caller
# cannot ask for a whole career in a single response.
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 50

# "Up next" is a strip on the profile, not a fixture list.
DEFAULT_UPCOMING_LIMIT = 5
MAX_UPCOMING_LIMIT = 50

# Bounds on ?year. Not product policy — a year outside this range overflows the
# date comparison in Postgres, and that would be a 500 for a typo.
MIN_YEAR = 1900
MAX_YEAR = 2100


def _service_error(tag, exc):
    """
    Map a service/selector exception onto the standard response envelope:
    ValidationError → 400, PermissionDenied → 403, NotFound → 404. The message
    the service wrote is what the client reads.
    """
    if isinstance(exc, ValidationError):
        flat = flatten_validation_error(exc.detail)
        logger.warning(f"{tag} | Validation Error | {flat['message']}")
        return response_data(
            success=False,
            message=flat["message"],
            status_code=400,
            error=flat["message"],
            data={"errors": flat["errors"]},
        )

    message = str(exc.detail)

    if isinstance(exc, PermissionDenied):
        logger.warning(f"{tag} | Forbidden | {message}")
        return response_data(
            success=False,
            message=message,
            status_code=403,
            data=error_body(message),
        )

    logger.info(f"{tag} | Not found | {message}")
    return response_data(
        success=False,
        message=message,
        status_code=404,
        data=error_body(message),
    )


# ─────────────────────────────────────────────
# QUERY PARAMETERS
#
# Each raises a DRF ValidationError so it lands in the same 400 envelope the
# body validation does — a caller should not be able to tell from the shape of
# the response whether their mistake was in the query string or the body.
# ─────────────────────────────────────────────

def _parse_int(raw, label):
    """One query parameter as an int, or a 400 naming it."""
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise ValidationError(f"{label} must be a whole number.")


def _parse_page(request):
    """
    ``?limit`` / ``?offset``, bounded.

    Junk is REJECTED (a 400 naming the parameter) but an over-large limit is
    CLAMPED to ``MAX_PAGE_SIZE`` rather than rejected. The two differ on
    purpose: "limit=banana" is a client bug worth surfacing, while "limit=200"
    is a client asking for more than it is going to get, and answering that with
    50 rows is more useful than answering with an error.
    """
    raw_limit = request.query_params.get("limit")
    raw_offset = request.query_params.get("offset")

    limit = (
        DEFAULT_PAGE_SIZE if raw_limit in (None, "")
        else _parse_int(raw_limit, "limit")
    )
    offset = (
        0 if raw_offset in (None, "")
        else _parse_int(raw_offset, "offset")
    )

    if limit < 1:
        raise ValidationError("limit must be at least 1.")

    if offset < 0:
        raise ValidationError("offset cannot be negative.")

    return min(limit, MAX_PAGE_SIZE), offset


def _parse_year(request):
    """``?year``, or None when the caller wants every season."""
    raw = request.query_params.get("year")

    if raw in (None, ""):
        return None

    year = _parse_int(raw, "year")

    if year < MIN_YEAR or year > MAX_YEAR:
        raise ValidationError(
            f"year must be between {MIN_YEAR} and {MAX_YEAR}."
        )

    return year


def _parse_sport_id(request, *, required=False):
    """
    ``?sport_id``, validated as a UUID.

    Existence is NOT checked here: on a list or a summary an unknown sport is
    an honest empty result. The stat-fields endpoint, where an unknown sport
    means the form has nothing to render, checks it for itself.
    """
    raw = request.query_params.get("sport_id")

    if raw in (None, ""):
        if required:
            raise ValidationError("sport_id is required.")
        return None

    if not is_valid_uuid(raw):
        raise ValidationError(f"'{raw}' is not a valid sport id.")

    return raw


def _parse_status(request):
    """``?status``, or None for both tabs at once."""
    raw = request.query_params.get("status")

    if raw in (None, ""):
        return None

    if raw not in MatchEntry.Status.values:
        raise ValidationError(
            "status must be one of: "
            + ", ".join(MatchEntry.Status.values)
            + "."
        )

    return raw


# ─────────────────────────────────────────────
# WRITES
# ─────────────────────────────────────────────

class CreateMatchAPIView(BaseAPIView):
    """POST /matches/create — log a played match, or schedule a fixture."""

    def post(self, request):
        TAG = "CreateMatchAPIView"
        try:
            # 403 before 400: a coach never gets told what was wrong with a
            # body they were not allowed to send. Same gate the service uses.
            user = MatchService.require_player(request.actor)

            serializer = MatchEntryCreateSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)

            match = MatchService.create_match(
                request.actor,
                **serializer.validated_data
            )

            logger.info(f"{TAG} | Match created | match_id={match.id}")

            return response_data(
                success=True,
                message="Match logged",
                status_code=201,
                # Re-read through the selector so the response is the stored
                # row with its joins, not the half-populated object the write
                # left in memory.
                data=MatchEntrySerializer(owned_match(user, match.id)).data,
            )

        except (ValidationError, PermissionDenied, NotFound) as e:
            return _service_error(TAG, e)

        except Exception as e:
            logger.exception(f"{TAG} | Error | {str(e)}")
            return response_data(
                success=False,
                message="Something went wrong",
                status_code=500,
            )


class UpdateMatchAPIView(BaseAPIView):
    """
    PATCH /matches/<match_id>/update — edit one of the player's own matches.

    The path this mainly serves is promoting a fixture: one call carrying
    ``status="played"`` alongside the result, minutes, rating and stats.
    """

    def patch(self, request, match_id):
        TAG = "UpdateMatchAPIView"
        try:
            user = MatchService.require_player(request.actor)

            serializer = MatchEntryUpdateSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)

            match = MatchService.update_match(
                request.actor,
                match_id,
                **serializer.validated_data
            )

            logger.info(f"{TAG} | Match updated | match_id={match.id}")

            return response_data(
                success=True,
                message="Match updated",
                data=MatchEntrySerializer(owned_match(user, match.id)).data,
            )

        except (ValidationError, PermissionDenied, NotFound) as e:
            return _service_error(TAG, e)

        except Exception as e:
            logger.exception(f"{TAG} | Error | {str(e)}")
            return response_data(
                success=False,
                message="Something went wrong",
                status_code=500,
            )


class DeleteMatchAPIView(BaseAPIView):
    """DELETE /matches/<match_id> — soft delete, owner only."""

    def delete(self, request, match_id):
        TAG = "DeleteMatchAPIView"
        try:
            match = MatchService.delete_match(request.actor, match_id)

            logger.info(f"{TAG} | Match deleted | match_id={match.id}")

            return response_data(
                success=True,
                message="Match deleted",
                data={"id": str(match.id)},
            )

        except (ValidationError, PermissionDenied, NotFound) as e:
            return _service_error(TAG, e)

        except Exception as e:
            logger.exception(f"{TAG} | Error | {str(e)}")
            return response_data(
                success=False,
                message="Something went wrong",
                status_code=500,
            )


# ─────────────────────────────────────────────
# READS
# ─────────────────────────────────────────────

class MatchListAPIView(BaseAPIView):
    """
    GET /matches/list — the signed-in player's diary, newest first.

    Filters: ``?status``, ``?year``, ``?sport_id``. Page: ``?limit``,
    ``?offset``.
    """

    def get(self, request):
        TAG = "MatchListAPIView"
        try:
            user = MatchService.require_player(request.actor)

            status = _parse_status(request)
            year = _parse_year(request)
            sport_id = _parse_sport_id(request)
            limit, offset = _parse_page(request)

            queryset = list_matches(
                user,
                status=status,
                year=year,
                sport_id=sport_id,
            )

            total = queryset.count()
            page = queryset[offset:offset + limit]

            return response_data(
                success=True,
                data={
                    "count": total,
                    "limit": limit,
                    "offset": offset,
                    "results": MatchEntrySerializer(page, many=True).data,
                },
            )

        except (ValidationError, PermissionDenied, NotFound) as e:
            return _service_error(TAG, e)

        except Exception as e:
            logger.exception(f"{TAG} | Error | {str(e)}")
            return response_data(
                success=False,
                message="Something went wrong",
                status_code=500,
            )


class UpcomingMatchesAPIView(BaseAPIView):
    """
    GET /matches/upcoming — the player's fixtures, next one first.

    Not a page: this is the "Up next" strip, so it takes a ``?limit`` and
    returns a flat list. A fixture whose date has passed is still here, flagged
    ``is_overdue`` — that is the prompt to go and log it.
    """

    def get(self, request):
        TAG = "UpcomingMatchesAPIView"
        try:
            user = MatchService.require_player(request.actor)

            raw_limit = request.query_params.get("limit")
            limit = (
                DEFAULT_UPCOMING_LIMIT if raw_limit in (None, "")
                else _parse_int(raw_limit, "limit")
            )

            if limit < 1:
                raise ValidationError("limit must be at least 1.")

            limit = min(limit, MAX_UPCOMING_LIMIT)

            fixtures = upcoming_matches(user, limit=limit)

            return response_data(
                success=True,
                data={
                    "count": len(fixtures),
                    "results": UpcomingMatchSerializer(
                        fixtures, many=True
                    ).data,
                },
            )

        except (ValidationError, PermissionDenied, NotFound) as e:
            return _service_error(TAG, e)

        except Exception as e:
            logger.exception(f"{TAG} | Error | {str(e)}")
            return response_data(
                success=False,
                message="Something went wrong",
                status_code=500,
            )


class MatchSummaryAPIView(BaseAPIView):
    """
    GET /matches/summary — the signed-in player's own season totals.

    Owner-only, and deliberately so: there is no ``username`` parameter and no
    other-user path, because in v1 there is no visibility model to consult and
    an endpoint that took a username would be one forgotten filter away from
    publishing every player's diary.

    In v1.1 the CV and the public profile attach here. When they do, this view
    is NOT the one they call: it stays the owner's endpoint, and the public
    surfaces get a visibility-aware sibling that reads
    ``MatchEntry.visibility`` and ``MatchDiarySettings.showcase_summary``.
    Widening this one instead would mean every existing caller silently gaining
    a viewer parameter it does not pass.

    Filters: ``?year``, ``?sport_id``.
    """

    def get(self, request):
        TAG = "MatchSummaryAPIView"
        try:
            user = MatchService.require_player(request.actor)

            summary = match_summary(
                user,
                year=_parse_year(request),
                sport_id=_parse_sport_id(request),
            )

            return response_data(
                success=True,
                data=MatchSummarySerializer(summary).data,
            )

        except (ValidationError, PermissionDenied, NotFound) as e:
            return _service_error(TAG, e)

        except Exception as e:
            logger.exception(f"{TAG} | Error | {str(e)}")
            return response_data(
                success=False,
                message="Something went wrong",
                status_code=500,
            )


class ShowcaseMatchSummaryAPIView(BaseAPIView):
    """
    GET /matches/summary/<username> — one player's summary, as somebody else
    sees it.

    This is what makes ``showcase_summary`` mean anything: without it the toggle
    is a switch wired to nothing. A player turns it on and their totals and
    streak become readable by other people in the app.

    AUTHENTICATED, not public-web. The diary is an in-app surface; the Sports CV
    is the thing that faces logged-out visitors, and it has its own opt-in, its
    own caching and its own safeguarding rules. Putting the diary on /public/
    would mean a second unauthenticated surface with a different toggle behind
    it, which is exactly how one of them ends up forgotten.

    404 for every refusal, and always the same one — see
    ``get_showcase_user``. A visitor must not be able to tell a player who
    switched the showcase off from a coach from a typo, because a distinct
    "showcase is off" response is a way to enumerate who has a diary.

    The owner reads their own regardless of the toggle: previewing your own
    profile should not require switching it on first.
    """

    def get(self, request, username):
        TAG = "ShowcaseMatchSummaryAPIView"
        try:
            viewer = (
                request.actor.user
                if request.actor and request.actor.is_user
                else None
            )

            resolved = get_showcase_user(username, viewer=viewer)

            if resolved is None:
                return response_data(
                    success=False,
                    message="Match summary not found",
                    status_code=404,
                    data=error_body("Match summary not found"),
                )

            owner, settings = resolved

            payload = match_summary(
                owner,
                year=_parse_year(request),
                sport_id=_parse_sport_id(request),
            )
            payload.update({
                "username": owner.username,
                "current_streak_weeks": settings.current_streak_weeks,
                "longest_streak_weeks": settings.longest_streak_weeks,
                "is_owner": viewer is not None and viewer.id == owner.id,
            })

            return response_data(
                success=True,
                data=ShowcaseMatchSummarySerializer(payload).data,
            )

        except (ValidationError, PermissionDenied, NotFound) as e:
            return _service_error(TAG, e)

        except Exception as e:
            logger.exception(f"{TAG} | Error | {str(e)}")
            return response_data(
                success=False,
                message="Something went wrong",
                status_code=500,
            )


class MatchStatFieldsAPIView(BaseAPIView):
    """
    GET /matches/stat-fields?sport_id=... — the catalog the quick-add form is
    built from.

    ``sport_id`` is required, and an unknown one is a 400 rather than an empty
    list: the client is asking "what can I log for this sport", and silence
    reads as "nothing", which is indistinguishable from a sport whose catalog
    was never seeded.
    """

    def get(self, request):
        TAG = "MatchStatFieldsAPIView"
        try:
            MatchService.require_player(request.actor)

            sport_id = _parse_sport_id(request, required=True)

            if not Sport.objects.filter(id=sport_id).exists():
                raise ValidationError("That sport does not exist.")

            fields = active_stat_fields(sport_id)

            return response_data(
                success=True,
                data={
                    "count": len(fields),
                    "results": MatchStatFieldSerializer(
                        fields, many=True
                    ).data,
                },
            )

        except (ValidationError, PermissionDenied, NotFound) as e:
            return _service_error(TAG, e)

        except Exception as e:
            logger.exception(f"{TAG} | Error | {str(e)}")
            return response_data(
                success=False,
                message="Something went wrong",
                status_code=500,
            )


# ─────────────────────────────────────────────
# SETTINGS
# ─────────────────────────────────────────────

class MatchDiarySettingsAPIView(BaseAPIView):
    """
    GET / PATCH /matches/settings — the signed-in player's own row.

    Self only: there is no id in the URL, so nobody can read or flip anybody
    else's diary settings. GET creates the row lazily, so a first-time player
    reads defaults rather than a 404.
    """

    def get(self, request):
        TAG = "MatchDiarySettingsAPIView"
        try:
            settings = DiarySettingsService.get_settings(request.actor)

            return response_data(
                success=True,
                data=MatchDiarySettingsSerializer(settings).data,
            )

        except (ValidationError, PermissionDenied, NotFound) as e:
            return _service_error(TAG, e)

        except Exception as e:
            logger.exception(f"{TAG} | Error | {str(e)}")
            return response_data(
                success=False,
                message="Something went wrong",
                status_code=500,
            )

    def patch(self, request):
        TAG = "UpdateMatchDiarySettingsAPIView"
        try:
            MatchService.require_player(request.actor)

            serializer = MatchDiarySettingsUpdateSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)

            settings = DiarySettingsService.update_settings(
                request.actor,
                **serializer.validated_data
            )

            return response_data(
                success=True,
                message="Diary settings updated",
                data=MatchDiarySettingsSerializer(settings).data,
            )

        except (ValidationError, PermissionDenied, NotFound) as e:
            return _service_error(TAG, e)

        except Exception as e:
            logger.exception(f"{TAG} | Error | {str(e)}")
            return response_data(
                success=False,
                message="Something went wrong",
                status_code=500,
            )
