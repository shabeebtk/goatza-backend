"""
Every page refusal in the standard envelope, with a code the client switches
on — the same shape as the squads and members screens.
"""

from rest_framework.exceptions import NotFound, PermissionDenied, Throttled

from apps.orgpages.errors import PageError
from core.views.base_views import BaseAPIView
from utils.errors import error_body
from utils.response import response_data

THROTTLED_MESSAGE = "Too many attempts. Please try again later."


def error_response(code, message, status_code, field=None, **extra):
    return response_data(
        success=False,
        message=message,
        data={"code": code, **error_body(message, field or "non_field_errors"), **extra},
        status_code=status_code,
    )


class PageErrorsMixin:
    """
    A permission refusal is mapped only when its detail is a plain sentence:
    the terms and guardian gates carry structured details of their own, and
    those must reach the client as they always have.
    """

    def handle_exception(self, exc):
        if isinstance(exc, PageError):
            return error_response(exc.code, exc.message, exc.status_code, exc.field)

        if isinstance(exc, PermissionDenied) and isinstance(exc.detail, str):
            return error_response("forbidden", str(exc.detail), 403)

        if isinstance(exc, NotFound):
            return error_response("not_found", str(exc.detail), 404)

        if isinstance(exc, Throttled):
            return error_response(
                "throttled", THROTTLED_MESSAGE, 429, retry_after=int(exc.wait or 0)
            )

        return super().handle_exception(exc)


class PageBaseView(PageErrorsMixin, BaseAPIView):
    pass


def body_of(request) -> dict:
    """The JSON body as a dict; anything else reads as empty and the service says why."""
    return request.data if isinstance(request.data, dict) else {}
