"""
The squad itself, as the club sees it. Thin: read the request, call the
service or selector, shape the answer.

Every route acts as the org through the X-Actor headers. The permission guard
runs BEFORE anything looks at the body — inside each service call, which checks
the permission first — so a coach gets 403, not a critique of a payload that
was never going to be accepted.
"""

from apps.organization.permissions import OrgPermission, has_org_permission
from apps.squads.errors import squad_not_found
from apps.squads.selectors.squad_selectors import org_squads, squad_detail
from apps.squads.serializers.squad_serializers import (
    SquadDetailSerializer,
    SquadListItemSerializer,
)
from apps.squads.services.guards import require_squads_permission
from apps.squads.services.squad_service import SquadService
from apps.squads.views.base import SquadBaseView
from utils.response import response_data


def body_of(request) -> dict:
    """The JSON body as a dict; anything else reads as empty and the service says why."""
    return request.data if isinstance(request.data, dict) else {}


def can_review(request) -> bool:
    actor = request.actor
    member = actor.organization_member if actor is not None and actor.is_org else None
    return has_org_permission(member, OrgPermission.SQUADS_REVIEW)


def detail_response(request, squad_id, *, status_code=200, message=""):
    """SquadDetail for ``squad_id``, read fresh so every annotation is current."""
    squad = squad_detail(request.actor.organization, squad_id)
    if squad is None:
        raise squad_not_found()

    return response_data(
        success=True,
        message=message,
        data=SquadDetailSerializer(
            squad, context={"can_review": can_review(request)}
        ).data,
        status_code=status_code,
    )


def invite_link_payload(squad):
    return {"url": SquadService.invite_url(squad), "enabled": squad.invite_enabled}


class SquadListAPIView(SquadBaseView):
    """GET /squads/list   ?sport_id=   → {squads: [SquadListItem]}"""

    def get(self, request):
        organization, _ = require_squads_permission(
            request.actor, OrgPermission.SQUADS_VIEW
        )
        squads = org_squads(organization, request.query_params.get("sport_id"))

        return response_data(
            success=True,
            data={"squads": SquadListItemSerializer(squads, many=True).data},
        )


class SquadCreateAPIView(SquadBaseView):
    """POST /squads/create   → 201 SquadDetail"""

    def post(self, request):
        squad = SquadService.create(request.actor, body_of(request))
        return detail_response(
            request, squad.id, status_code=201, message="Squad created"
        )


class SquadDetailAPIView(SquadBaseView):
    """GET /squads/<uuid>   → SquadDetail"""

    def get(self, request, squad_id):
        require_squads_permission(request.actor, OrgPermission.SQUADS_VIEW)
        return detail_response(request, squad_id)


class SquadUpdateAPIView(SquadBaseView):
    """PATCH /squads/<uuid>/update   (partial)   → SquadDetail"""

    def patch(self, request, squad_id):
        SquadService.update(request.actor, squad_id, body_of(request))
        return detail_response(request, squad_id, message="Squad updated")


class SquadDeleteAPIView(SquadBaseView):
    """POST /squads/<uuid>/delete"""

    def post(self, request, squad_id):
        SquadService.delete(request.actor, squad_id)
        return response_data(success=True, message="Squad deleted")


class SquadPhotoAPIView(SquadBaseView):
    """POST /squads/<uuid>/photo   {photo_url, photo_public_id} | {remove: true}   → SquadDetail"""

    def post(self, request, squad_id):
        body = body_of(request)

        if body.get("remove") is True:
            SquadService.remove_photo(request.actor, squad_id)
            return detail_response(request, squad_id, message="Photo removed")

        SquadService.set_photo(
            request.actor,
            squad_id,
            body.get("photo_url"),
            body.get("photo_public_id"),
        )
        return detail_response(request, squad_id, message="Photo updated")


class SquadInviteLinkAPIView(SquadBaseView):
    """PATCH /squads/<uuid>/invite-link   {enabled}   → {url, enabled}"""

    def patch(self, request, squad_id):
        squad = SquadService.set_invite_enabled(
            request.actor, squad_id, body_of(request).get("enabled")
        )
        return response_data(success=True, data=invite_link_payload(squad))


class SquadInviteLinkResetAPIView(SquadBaseView):
    """POST /squads/<uuid>/invite-link/reset   → {url, enabled}"""

    def post(self, request, squad_id):
        squad = SquadService.reset_invite_link(request.actor, squad_id)
        return response_data(
            success=True,
            message="Invite link reset",
            data=invite_link_payload(squad),
        )


class SquadStaffAddAPIView(SquadBaseView):
    """POST /squads/<uuid>/staff   {member_id, role, title?}   → 201 SquadDetail"""

    def post(self, request, squad_id):
        body = body_of(request)
        SquadService.add_staff(
            request.actor,
            squad_id,
            body.get("member_id"),
            body.get("role"),
            body.get("title", ""),
        )
        return detail_response(
            request, squad_id, status_code=201, message="Staff added"
        )


class SquadStaffRemoveAPIView(SquadBaseView):
    """POST /squads/<uuid>/staff/<uuid>/remove   → SquadDetail"""

    def post(self, request, squad_id, staff_id):
        SquadService.remove_staff(request.actor, squad_id, staff_id)
        return detail_response(request, squad_id, message="Staff removed")
