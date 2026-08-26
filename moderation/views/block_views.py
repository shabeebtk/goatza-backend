"""
HTTP entry points for blocking, mounted under /moderation/.

Thin by design: these resolve the target, hand it to BlockService, and map the
outcome onto the standard envelope. Every rule — self-block, the org owner/admin
gate, follow teardown, cache invalidation — lives in the service, because the
same rules have to hold for the internal callers (messaging, feed, recruitments)
that will reach BlockService directly and never pass through a view.
"""

import logging

from django.http import Http404
from django.shortcuts import get_object_or_404
from rest_framework.exceptions import (
    NotFound,
    PermissionDenied,
    ValidationError,
)

from accounts.models import User
from core.constant import TYPE_ORGANIZATION, TYPE_USER
from core.views.base_views import BaseAPIView
from moderation.selectors.block_selectors import BlockSelector
from moderation.serializers.block_serializers import BlockedItemSerializer
from moderation.services.block_services import BlockService
from organization.models import Organization
from utils.errors import error_body, flatten_validation_error
from utils.response import response_data

logger = logging.getLogger(__name__)


def _service_error(tag, exc):
    """
    Map a service/selector exception onto the standard response envelope:
    ValidationError -> 400, PermissionDenied -> 403, NotFound -> 404. The
    message the service wrote is what the client reads.
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


class BlockAPIView(BaseAPIView):
    """
    POST   /moderation/block — block an account.
    DELETE /moderation/block — unblock it.

    One URL, two verbs, one body: ``{"target_type", "target_id"}``. The pair is
    a toggle in the UI, and both halves are idempotent in the service, so the
    client can fire either without first reading the current state.
    """

    def post(self, request):
        TAG = "BlockAPIView.post"
        try:
            target_user, target_org, error = self._resolve_target(request.data)
            if error:
                return error

            success, result = BlockService.block(
                actor=request.actor,
                target_user=target_user,
                target_org=target_org,
            )

            if not success:
                return response_data(
                    success=False,
                    message=result,
                    status_code=400,
                    data=error_body(result),
                )

            return response_data(
                success=True,
                message="Blocked successfully",
                data=result,
            )

        except Http404:
            # get_object_or_404 raises Django's Http404, which is NOT a DRF
            # exception — without this it falls into the broad handler below
            # and an unknown target id comes back as a 500.
            return response_data(
                success=False,
                message="Target not found",
                status_code=404,
                data=error_body("Target not found"),
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

    def delete(self, request):
        TAG = "BlockAPIView.delete"
        try:
            target_user, target_org, error = self._resolve_target(request.data)
            if error:
                return error

            success, result = BlockService.unblock(
                actor=request.actor,
                target_user=target_user,
                target_org=target_org,
            )

            if not success:
                return response_data(
                    success=False,
                    message=result,
                    status_code=400,
                    data=error_body(result),
                )

            return response_data(
                success=True,
                message="Unblocked successfully",
                data=result,
            )

        except Http404:
            # get_object_or_404 raises Django's Http404, which is NOT a DRF
            # exception — without this it falls into the broad handler below
            # and an unknown target id comes back as a 500.
            return response_data(
                success=False,
                message="Target not found",
                status_code=404,
                data=error_body("Target not found"),
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

    def _resolve_target(self, data):
        """
        ``(target_user, target_org, error_response)`` from the request body —
        the same target_type/target_id pair FollowAPIView takes, so the client
        sends one shape for follow and block alike.
        """
        target_type = data.get("target_type")
        target_id = data.get("target_id")

        if target_type not in [TYPE_USER, TYPE_ORGANIZATION] or not target_id:
            message = "target_type and target_id are required"
            return None, None, response_data(
                success=False,
                message=message,
                status_code=400,
                data=error_body(message),
            )

        if target_type == TYPE_USER:
            return get_object_or_404(User, id=target_id), None, None

        return None, get_object_or_404(Organization, id=target_id), None


class BlockedListAPIView(BaseAPIView):
    """
    GET /moderation/blocked — the acting actor's blocked accounts, newest first.

    Paginated with limit/offset like every other list in the app; a settings
    screen that has to ship an unbounded list on open is the one that stops
    being openable.
    """

    DEFAULT_LIMIT = 20
    MAX_LIMIT = 50

    def get(self, request):
        TAG = "BlockedListAPIView"
        try:
            limit, offset = self._page(request)

            page, total = BlockSelector.get_blocked_list(
                actor=request.actor,
                limit=limit,
                offset=offset,
            )

            rows = list(page)

            return response_data(
                success=True,
                data={
                    "count": total,
                    "limit": limit,
                    "offset": offset,
                    "has_more": offset + len(rows) < total,
                    "results": BlockedItemSerializer(rows, many=True).data,
                },
            )

        except Http404:
            # get_object_or_404 raises Django's Http404, which is NOT a DRF
            # exception — without this it falls into the broad handler below
            # and an unknown target id comes back as a 500.
            return response_data(
                success=False,
                message="Target not found",
                status_code=404,
                data=error_body("Target not found"),
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

    def _page(self, request):
        """Clamp limit/offset so a client can't ask for the whole table."""
        def as_int(name, default):
            try:
                return int(request.query_params.get(name, default))
            except (TypeError, ValueError):
                return default

        limit = as_int("limit", self.DEFAULT_LIMIT)
        offset = as_int("offset", 0)

        limit = max(1, min(limit, self.MAX_LIMIT))
        offset = max(0, offset)

        return limit, offset
