"""
The one refusal type for the org page: a stable code the client switches on
and a human sentence it can show.

The codes are a contract with the frontend. Add one; never rename one.

    not_found, forbidden, switch_to_org, invalid, no_sport, program_limit,
    coach_limit, player_limit, photo_limit, already_added, not_eligible,
    invalid_photo, throttled

``invalid`` always names its field (``errors[field]``): a bad value, an
unknown section key, an unknown facility key, a reorder that is not exactly
the org's rows.

``forbidden`` and ``throttled`` are produced by the views' error mixin from
DRF's own exceptions; everything else is raised as a PageError.
"""


class PageError(Exception):
    """
    ``field`` names the request field the sentence is about, when there is
    one, so a form can put it under the right input. It rides in the standard
    ``errors`` body; the code is what the client branches on.
    """

    def __init__(self, code, message, status_code=400, field=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.field = field


NO_SPORT_MESSAGE = "Add a sport to your page first."
NOT_ELIGIBLE_MESSAGE = "This player can't be featured."
INVALID_PHOTO_MESSAGE = "This photo couldn't be used. Upload it again."
REORDER_MESSAGE = "The list changed. Refresh and try again."


def invalid(message, field):
    return PageError("invalid", message, field=field)


def not_found(message="This wasn't found."):
    return PageError("not_found", message, status_code=404)


def no_sport():
    return PageError("no_sport", NO_SPORT_MESSAGE, field="sport_id")


def not_eligible():
    return PageError("not_eligible", NOT_ELIGIBLE_MESSAGE, field="user_id")


def invalid_photo():
    return PageError("invalid_photo", INVALID_PHOTO_MESSAGE, field="photo_url")
