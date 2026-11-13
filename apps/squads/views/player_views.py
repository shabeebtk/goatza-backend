"""
The player's side: the invite link (its page and the join), and "my squads".

The link's page is public — anyone the club shared it with can open it — and
answers ONE generic 404 for every miss: an unknown token, a link turned off, a
deleted squad, an org that cannot be joined, and, for a signed-in caller, a
block either way. It never lists players.

Everything else here acts as the person (``switch_to_personal`` otherwise).
"""

from apps.accounts.models import User
from apps.guardians.permissions import BLOCKING_STATUSES
from apps.moderation.services.block_guard import is_blocked
from apps.squads.errors import SquadError
from apps.squads.selectors.player_selectors import (
    head_coach_for,
    live_row_for,
    my_squad_row,
    my_squads,
    squad_for_invite_token,
)
from apps.squads.serializers.player_serializers import (
    invite_page_payload,
    my_squad_item,
)
from apps.squads.services.guards import require_user_actor
from apps.squads.services.membership_service import MembershipService
from apps.squads.throttles import SquadInviteTokenThrottle, SquadJoinThrottle
from apps.squads.views.base import SquadBaseView, SquadErrorsMixin
from core.views.base_views import PublicAPIView
from utils.response import response_data

LINK_NOT_FOUND_MESSAGE = "This invite link isn't valid."


def _link_not_found():
    return SquadError("not_found", LINK_NOT_FOUND_MESSAGE, status_code=404)


def _me(actor, user, squad):
    """
    Where the signed-in person stands with this squad, and whether the Join
    button works for them. ``reason`` says why it doesn't, when the answer is
    something they can act on; a child waiting on a parent is told by the
    guardian gate, not here.
    """
    row = live_row_for(squad, user)
    state = row.status if row is not None else "none"

    reason = None
    if user.role != User.Role.PLAYER:
        reason = "not_a_player"
    elif not actor.is_user:
        reason = "switch_to_personal"

    can_join = (
        reason is None
        and state in ("none", "invited")
        and user.guardian_consent_status not in BLOCKING_STATUSES
    )

    return {
        "state": state,
        "member_id": str(row.id) if row is not None else None,
        "can_join": can_join,
        "reason": reason,
    }


class SquadInvitePageAPIView(SquadErrorsMixin, PublicAPIView):
    """GET /squads/invite/<token> — public."""

    throttle_classes = [SquadInviteTokenThrottle]

    def get(self, request, token):
        squad = squad_for_invite_token(token)
        if squad is None:
            raise _link_not_found()

        me = None
        actor = request.actor
        if actor is not None:
            if is_blocked(request.user, squad.organization):
                raise _link_not_found()
            me = _me(actor, request.user, squad)

        return response_data(
            success=True,
            data=invite_page_payload(squad, head_coach_for(squad, actor), me),
        )


class SquadJoinAPIView(SquadBaseView):
    """
    POST /squads/invite/<token>/join — the person asks to join.

    201 for a new request; 200 for one that already existed, or when the tap
    accepted an open invite (state "active"). ``{state, member_id}``.
    """

    throttle_classes = [SquadInviteTokenThrottle, SquadJoinThrottle]

    def post(self, request, token):
        user = require_user_actor(request.actor)

        squad = squad_for_invite_token(token)
        if squad is None or is_blocked(user, squad.organization):
            raise _link_not_found()

        row, created = MembershipService.request_join(user, squad)

        return response_data(
            success=True,
            message="Request sent" if created else "",
            data={"state": row.status, "member_id": str(row.id)},
            status_code=201 if created else 200,
        )


class MySquadsAPIView(SquadBaseView):
    """GET /squads/mine — the person's squads, invites and requests."""

    def get(self, request):
        user = require_user_actor(request.actor)
        return response_data(
            success=True,
            data={"items": [my_squad_item(row) for row in my_squads(user)]},
        )


class MySquadAcceptAPIView(SquadBaseView):
    """POST /squads/mine/<uuid>/accept — say yes to an invite."""

    def post(self, request, member_id):
        user = require_user_actor(request.actor)
        row = MembershipService.accept_invite(user, member_id)
        return response_data(
            success=True,
            message="You're in",
            data=my_squad_item(my_squad_row(user, row.id)),
        )


class MySquadDeclineAPIView(SquadBaseView):
    """POST /squads/mine/<uuid>/decline — say no to an invite. Nobody is told."""

    def post(self, request, member_id):
        user = require_user_actor(request.actor)
        MembershipService.decline_invite(user, member_id)
        return response_data(success=True, message="Invite declined")


class MySquadWithdrawAPIView(SquadBaseView):
    """POST /squads/mine/<uuid>/withdraw — take back a request."""

    def post(self, request, member_id):
        user = require_user_actor(request.actor)
        MembershipService.withdraw_request(user, member_id)
        return response_data(success=True, message="Request withdrawn")


class MySquadLeaveAPIView(SquadBaseView):
    """POST /squads/mine/<uuid>/leave — leave a squad. The career stint is closed, not erased."""

    def post(self, request, member_id):
        user = require_user_actor(request.actor)
        MembershipService.leave(user, member_id)
        return response_data(success=True, message="You left the squad")
