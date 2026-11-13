"""
The players in a squad, as the club sees them: the roster, link requests,
direct invites, and changes to a player's place. Thin, like squad_views; the
permission is checked first inside every service call.
"""

from apps.organization.permissions import OrgPermission
from apps.squads.selectors.squad_selectors import (
    members_for_response,
    open_invites,
    pending_requests,
    primary_positions_for,
    roster,
)
from apps.squads.serializers.squad_serializers import (
    MemberItemSerializer,
    approve_payload,
    candidate_payload,
    requests_payload,
    roster_payload,
)
from apps.squads.services.guards import get_org_squad, require_squads_permission
from apps.squads.services.membership_service import MembershipService
from apps.squads.throttles import SquadDirectInviteThrottle
from apps.squads.views.base import SquadBaseView
from apps.squads.views.squad_views import body_of, can_review
from utils.response import response_data


def member_items(request, rows) -> list:
    """MemberItem for each row, read fresh and joined in one query."""
    rows = members_for_response([row.id for row in rows])
    if not rows:
        return []

    primaries = primary_positions_for(
        {row.user_id for row in rows}, rows[0].squad.sport_id
    )
    return MemberItemSerializer(
        rows,
        many=True,
        context={"can_review": can_review(request), "primary_positions": primaries},
    ).data


def member_response(request, row, *, status_code=200, message=""):
    items = member_items(request, [row])
    return response_data(
        success=True,
        message=message,
        data=items[0] if items else {},
        status_code=status_code,
    )


class SquadRosterAPIView(SquadBaseView):
    """GET /squads/<uuid>/members   → Roster"""

    def get(self, request, squad_id):
        organization, _ = require_squads_permission(
            request.actor, OrgPermission.SQUADS_VIEW
        )
        squad = get_org_squad(organization, squad_id)

        return response_data(
            success=True,
            data=roster_payload(roster(squad), can_review=can_review(request)),
        )


class SquadMemberUpdateAPIView(SquadBaseView):
    """PATCH /squads/<uuid>/members/<uuid>   {position_id?, jersey_number?, is_captain?, notes?}   → MemberItem"""

    def patch(self, request, squad_id, member_id):
        row = MembershipService.update_member(
            request.actor, squad_id, member_id, body_of(request)
        )
        return member_response(request, row, message="Player updated")


class SquadMemberRemoveAPIView(SquadBaseView):
    """POST /squads/<uuid>/members/<uuid>/remove   {reason: "left" | "mistake"}"""

    def post(self, request, squad_id, member_id):
        MembershipService.remove(
            request.actor, squad_id, member_id, body_of(request).get("reason")
        )
        return response_data(success=True, message="Player removed")


class SquadMemberMoveAPIView(SquadBaseView):
    """POST /squads/<uuid>/members/<uuid>/move   {to_squad_id}   → MemberItem (the new row)"""

    def post(self, request, squad_id, member_id):
        row = MembershipService.move(
            request.actor, squad_id, member_id, body_of(request).get("to_squad_id")
        )
        return member_response(request, row, message="Player moved")


class SquadRequestsAPIView(SquadBaseView):
    """GET /squads/<uuid>/requests   → Requests"""

    def get(self, request, squad_id):
        organization, _ = require_squads_permission(
            request.actor, OrgPermission.SQUADS_REVIEW
        )
        squad = get_org_squad(organization, squad_id)

        requests = pending_requests(squad)
        primaries = primary_positions_for(
            {row.user_id for row in requests}, squad.sport_id
        )

        return response_data(
            success=True,
            data=requests_payload(requests, open_invites(squad), primaries),
        )


class SquadRequestsApproveAPIView(SquadBaseView):
    """POST /squads/<uuid>/requests/approve   {items: [{member_id, position_id?, jersey_number?, is_captain?}]}"""

    def post(self, request, squad_id):
        result = MembershipService.approve(
            request.actor, squad_id, body_of(request).get("items")
        )

        approved = members_for_response([row.id for row in result["approved"]])
        primaries = primary_positions_for(
            {row.user_id for row in approved},
            approved[0].squad.sport_id if approved else None,
        )

        return response_data(
            success=True,
            data=approve_payload(
                {"approved": approved, "failed": result["failed"]},
                can_review=can_review(request),
                primary_positions=primaries,
            ),
        )


class SquadRequestsDeclineAPIView(SquadBaseView):
    """POST /squads/<uuid>/requests/decline   {member_ids: [...]}   → {declined, failed}"""

    def post(self, request, squad_id):
        result = MembershipService.decline(
            request.actor, squad_id, body_of(request).get("member_ids")
        )
        return response_data(success=True, data=result)


class SquadCandidatesAPIView(SquadBaseView):
    """GET /squads/<uuid>/candidates?q=   → {results: [Candidate]}"""

    def get(self, request, squad_id):
        candidates = MembershipService.candidates(
            request.actor, squad_id, request.query_params.get("q", "")
        )
        return response_data(
            success=True,
            data={"results": [candidate_payload(c) for c in candidates]},
        )


class SquadInviteCreateAPIView(SquadBaseView):
    """
    POST /squads/<uuid>/invites   {user_id, position_id?, jersey_number?, application_id?}
    → 201 MemberItem. INVITED — or ACTIVE, when the player had already asked.
    """

    throttle_classes = [SquadDirectInviteThrottle]

    def post(self, request, squad_id):
        body = body_of(request)
        row = MembershipService.invite(
            request.actor,
            squad_id,
            body.get("user_id"),
            position_id=body.get("position_id"),
            jersey_number=body.get("jersey_number"),
            application_id=body.get("application_id"),
        )
        added = row.status == row.Status.ACTIVE
        return member_response(
            request,
            row,
            status_code=201,
            message="Player added" if added else "Invite sent",
        )


class SquadInviteCancelAPIView(SquadBaseView):
    """POST /squads/<uuid>/invites/<uuid>/cancel"""

    def post(self, request, squad_id, member_id):
        MembershipService.cancel_invite(request.actor, squad_id, member_id)
        return response_data(success=True, message="Invite cancelled")
