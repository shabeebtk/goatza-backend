"""
HTTP entry points for highlights (HIGHLIGHTS_SPEC.md §2), mounted at
/api/highlights/.

Thin by design: resolve what the URL names, hand the actor and the validated
body to HighlightService / the selectors, and shape the answer. The
"players only, acting as themselves" rule is NOT re-checked here — the service
owns it and raises PermissionDenied, which ``_service_error`` turns into the
standard 403 envelope. One rule, one place.
"""

import logging

from rest_framework.exceptions import (
    NotFound,
    PermissionDenied,
    ValidationError,
)

from accounts.models import User
from core.views.base_views import BaseAPIView
from highlights.models import Highlight
from highlights.selectors.highlight_selectors import (
    is_owner,
    visible_highlights_for,
)
from highlights.serializers.highlight_serializers import (
    HighlightCreateSerializer,
    HighlightReorderSerializer,
    HighlightSerializer,
    HighlightUpdateSerializer,
)
from highlights.services.highlight_services import HighlightService
from utils.errors import error_body, flatten_validation_error
from utils.response import response_data

logger = logging.getLogger(__name__)


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


class CreateHighlightAPIView(BaseAPIView):
    """POST /api/highlights/ — direct upload or promote from a post."""

    def post(self, request):
        TAG = "CreateHighlightAPIView"
        try:
            # 403 before 400: a non-player never gets told what was wrong with
            # a body they were not allowed to send. Same gate the service uses.
            HighlightService.require_player(request.actor)

            serializer = HighlightCreateSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)

            highlight = HighlightService.create_highlight(
                request.actor,
                payload=serializer.validated_data
            )

            logger.info(f"{TAG} | Highlight created | highlight_id={highlight.id}")

            return response_data(
                success=True,
                message="Highlight added",
                status_code=201,
                data=HighlightSerializer(
                    highlight,
                    context={"is_owner": True}
                ).data,
            )

        except (ValidationError, PermissionDenied, NotFound) as e:
            return _service_error(TAG, e)

        except Exception as e:
            logger.error(f"{TAG} | Error | {str(e)}")
            return response_data(
                success=False,
                message="Something went wrong",
                status_code=500,
                error=str(e)
            )


class UserHighlightListAPIView(BaseAPIView):
    """GET /api/highlights/user/<username>/ — the rail this actor may see."""

    def get(self, request, username):
        TAG = "UserHighlightListAPIView"
        try:
            owner = (
                User.objects
                .filter(username=username)
                .first()
            )

            if owner is None:
                return response_data(
                    success=False,
                    message="User not found",
                    status_code=404
                )

            actor = request.actor
            owner_view = is_owner(owner, actor)

            highlights = list(visible_highlights_for(owner, actor))

            return response_data(
                success=True,
                data={
                    "count": len(highlights),
                    "is_owner": owner_view,
                    "results": HighlightSerializer(
                        highlights,
                        many=True,
                        context={"is_owner": owner_view},
                    ).data,
                },
            )

        except Exception as e:
            logger.error(f"{TAG} | Error | {str(e)}")
            return response_data(
                success=False,
                message="Something went wrong",
                status_code=500,
                error=str(e)
            )


class HighlightDetailAPIView(BaseAPIView):
    """PATCH / DELETE /api/highlights/<highlight_id>/ — owner only."""

    def patch(self, request, highlight_id):
        TAG = "UpdateHighlightAPIView"
        try:
            HighlightService.require_player(request.actor)

            serializer = HighlightUpdateSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)

            highlight = HighlightService.update_highlight(
                request.actor,
                highlight_id,
                **serializer.validated_data
            )

            return response_data(
                success=True,
                message="Highlight updated",
                data=HighlightSerializer(
                    highlight,
                    context={"is_owner": True}
                ).data,
            )

        except (ValidationError, PermissionDenied, NotFound) as e:
            return _service_error(TAG, e)

        except Exception as e:
            logger.error(f"{TAG} | Error | {str(e)}")
            return response_data(
                success=False,
                message="Something went wrong",
                status_code=500,
                error=str(e)
            )

    def delete(self, request, highlight_id):
        TAG = "DeleteHighlightAPIView"
        try:
            highlight = HighlightService.delete_highlight(
                request.actor,
                highlight_id
            )

            logger.info(f"{TAG} | Highlight deleted | highlight_id={highlight.id}")

            return response_data(
                success=True,
                message="Highlight deleted",
                data={"id": str(highlight.id)}
            )

        except (ValidationError, PermissionDenied, NotFound) as e:
            return _service_error(TAG, e)

        except Exception as e:
            logger.error(f"{TAG} | Error | {str(e)}")
            return response_data(
                success=False,
                message="Something went wrong",
                status_code=500,
                error=str(e)
            )


class ReorderHighlightsAPIView(BaseAPIView):
    """PUT /api/highlights/reorder/ — {"ordered_ids": [...]}, the whole rail."""

    def put(self, request):
        TAG = "ReorderHighlightsAPIView"
        try:
            HighlightService.require_player(request.actor)

            serializer = HighlightReorderSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)

            highlights = HighlightService.reorder_highlights(
                request.actor,
                serializer.validated_data["ordered_ids"]
            )

            return response_data(
                success=True,
                message="Highlights reordered",
                data={
                    "count": len(highlights),
                    "results": HighlightSerializer(
                        highlights,
                        many=True,
                        context={"is_owner": True},
                    ).data,
                },
            )

        except (ValidationError, PermissionDenied, NotFound) as e:
            return _service_error(TAG, e)

        except Exception as e:
            logger.error(f"{TAG} | Error | {str(e)}")
            return response_data(
                success=False,
                message="Something went wrong",
                status_code=500,
                error=str(e)
            )


class RecordHighlightViewAPIView(BaseAPIView):
    """
    POST /api/highlights/<highlight_id>/view/ — fire-and-forget view counter.

    Open to any authenticated actor, but only for a clip they are actually
    allowed to see: the selector decides, and a clip they cannot see reads as
    404 rather than admitting it exists.
    """

    def post(self, request, highlight_id):
        TAG = "RecordHighlightViewAPIView"
        try:
            highlight = (
                Highlight.objects
                .select_related("user")
                .filter(id=highlight_id, is_deleted=False)
                .first()
            )

            if highlight is None:
                return response_data(
                    success=False,
                    message="Highlight not found.",
                    status_code=404
                )

            visible = visible_highlights_for(
                highlight.user,
                request.actor
            ).filter(id=highlight.id).exists()

            if not visible:
                return response_data(
                    success=False,
                    message="Highlight not found.",
                    status_code=404
                )

            counted = HighlightService.record_view(request.actor, highlight)

            return response_data(
                success=True,
                message="View recorded" if counted else "View not counted",
                data={"counted": counted}
            )

        except Exception as e:
            logger.error(f"{TAG} | Error | {str(e)}")
            return response_data(
                success=False,
                message="Something went wrong",
                status_code=500,
                error=str(e)
            )
