# core/decorators/actor_required.py

from functools import wraps
from utils.response import response_data


def org_required(view_func):

    @wraps(view_func)
    def wrapper(view, request, *args, **kwargs):

        actor = getattr(request, "actor", None)

        if not actor or not actor.is_org:
            return response_data(
                success=False,
                message="Organization access required",
                status_code=403
            )

        return view_func(view, request, *args, **kwargs)

    return wrapper


def player_required(view_func):
    """
    User-actor counterpart of @org_required: the request must be acting as a
    user (not an organization) whose role is 'player'. Same rule the detail
    serializer's can_apply uses. Blocks org actors and non-player users with
    a 403 before the view body runs.
    """

    @wraps(view_func)
    def wrapper(view, request, *args, **kwargs):

        actor = getattr(request, "actor", None)

        if not actor or not actor.is_user:
            return response_data(
                success=False,
                message="Player access required",
                status_code=403
            )

        if actor.user.role != "player":
            return response_data(
                success=False,
                message="Only players can perform this action",
                status_code=403
            )

        return view_func(view, request, *args, **kwargs)

    return wrapper