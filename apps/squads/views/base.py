"""
Every squads refusal in the standard envelope, with a code the client switches
on — the same shape as the members screens (OrgMemberErrorsMixin).
"""

from rest_framework.exceptions import NotFound, PermissionDenied, Throttled

from apps.squads.errors import SquadError
from core.views.base_views import BaseAPIView
from utils.errors import error_body
from utils.response import response_data

THROTTLED_MESSAGE = "Too many attempts. Please try again later."


def _error(code, message, status_code, field=None):
    return response_data(
        success=False,
        message=message,
        data={"code": code, **error_body(message, field or "non_field_errors")},
        status_code=status_code,
    )


def squad_error_response(exc):
    """A SquadError in the standard envelope — for a view outside squads (careers' verify)."""
    return _error(exc.code, exc.message, exc.status_code, exc.field)


class SquadErrorsMixin:
    """
    A permission refusal is mapped only when its detail is a plain sentence:
    the terms and guardian gates carry structured details of their own, and
    those must reach the client as they always have.
    """

    def handle_exception(self, exc):
        if isinstance(exc, SquadError):
            return _error(exc.code, exc.message, exc.status_code, exc.field)

        if isinstance(exc, PermissionDenied) and isinstance(exc.detail, str):
            return _error("forbidden", str(exc.detail), 403)

        if isinstance(exc, NotFound):
            return _error("not_found", str(exc.detail), 404)

        if isinstance(exc, Throttled):
            return response_data(
                success=False,
                message=THROTTLED_MESSAGE,
                data={
                    "code": "throttled",
                    **error_body(THROTTLED_MESSAGE),
                    "retry_after": int(exc.wait or 0),
                },
                status_code=429,
            )

        return super().handle_exception(exc)


class SquadBaseView(SquadErrorsMixin, BaseAPIView):
    pass
