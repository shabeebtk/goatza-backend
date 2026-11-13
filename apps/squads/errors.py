"""
The one refusal type for squads: a stable code the client switches on and a
human sentence it can show.

The codes are a contract with the frontend. Add one; never rename one.

    not_found, forbidden, switch_to_org, switch_to_personal, squad_limit,
    name_taken, invalid_name, invalid_sport, sport_locked, invalid_field,
    invalid_photo, staff_limit, already_staff, invalid_staff, member_limit,
    request_limit, invite_limit, already_member, already_requested,
    already_invited, not_a_player, cannot_add, invalid_position,
    invalid_jersey, jersey_taken, not_pending, not_active, invalid_reason,
    sport_mismatch, same_squad, invite_closed, invalid_application, throttled

``forbidden`` and ``throttled`` are produced by the views' error mixin from
DRF's own exceptions; everything else is raised as a SquadError.
"""


class SquadError(Exception):
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


SQUAD_NOT_FOUND_MESSAGE = "This squad wasn't found."
MEMBER_NOT_FOUND_MESSAGE = "This player wasn't found in the squad."

# A deactivated account, one waiting to be deleted, a child waiting on a parent
# and a block in either direction all read the same: the club must never learn
# which it was.
CANNOT_ADD_MESSAGE = "Couldn't add this player."

INVITE_CLOSED_MESSAGE = "This invite isn't available any more."


def squad_not_found():
    return SquadError("not_found", SQUAD_NOT_FOUND_MESSAGE, status_code=404)


def member_not_found():
    return SquadError("not_found", MEMBER_NOT_FOUND_MESSAGE, status_code=404)


def cannot_add():
    return SquadError("cannot_add", CANNOT_ADD_MESSAGE)


def invite_closed():
    return SquadError("invite_closed", INVITE_CLOSED_MESSAGE)
