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