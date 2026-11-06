"""
The members screens' endpoints. Thin: read the request, call
OrganizationMemberService, shape the answer.

Org-scoped routes rely on the X-Actor headers (``request.actor``). An invite's
own routes act as the invited person, and the token ones only need a login: the
address is what is checked there, and it is the same whichever hat they wear.
"""

from rest_framework.exceptions import PermissionDenied, Throttled

from apps.organization.permissions import member_permissions
from apps.organization.serializers.organization_member_serializers import (
    InviteCreateSerializer,
    MyInviteSerializer,
    OrganizationMemberSerializer,
    PendingInviteSerializer,
    RoleChangeSerializer,
)
from apps.organization.serializers.organization_serializers import (
    MyOrganizationSerializer,
)
from apps.organization.services.organization_member_service import (
    SWITCH_TO_PERSONAL_MESSAGE,
    OrganizationMemberService,
    OrgMemberError,
)
from apps.organization.throttles import OrgInviteCreateThrottle, OrgInviteTokenThrottle
from core.views.base_views import BaseAPIView, PublicAPIView
from utils.errors import error_body
from utils.response import response_data

THROTTLED_MESSAGE = "Too many attempts. Please try again later."


def _error(code, message, status_code):
    return response_data(
        success=False,
        message=message,
        data={"code": code, **error_body(message)},
        status_code=status_code,
    )


class OrgMemberErrorsMixin:
    """
    Every refusal in the standard envelope with a code the client switches on.

    A permission refusal only when its detail is a plain sentence: the terms and
    guardian gates carry structured details of their own, and those must reach
    the client as they always have.
    """

    def handle_exception(self, exc):
        if isinstance(exc, OrgMemberError):
            return _error(exc.code, exc.message, exc.status_code)

        if isinstance(exc, PermissionDenied) and isinstance(exc.detail, str):
            return _error("forbidden", str(exc.detail), 403)

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


class OrgMemberBaseView(OrgMemberErrorsMixin, BaseAPIView):
    pass


def _organization_payload(organization, member):
    return MyOrganizationSerializer(
        organization,
        context={"memberships_by_org": {organization.id: member}},
    ).data


def _require_user_actor(request):
    if not request.actor.is_user:
        raise PermissionDenied(SWITCH_TO_PERSONAL_MESSAGE)
    return request.user


class OrganizationMembersAPIView(OrgMemberBaseView):
    """GET /organizations/members"""

    def get(self, request):
        members = OrganizationMemberService.list_members(request.actor)
        me = request.actor.organization_member

        return response_data(
            success=True,
            data={
                "members": OrganizationMemberSerializer(
                    members, many=True, context={"user_id": request.user.id}
                ).data,
                "me": {
                    "role": me.role,
                    "permissions": sorted(member_permissions(me)),
                },
            },
        )


class OrganizationPendingInvitesAPIView(OrgMemberBaseView):
    """GET /organizations/members/invites"""

    def get(self, request):
        invites = OrganizationMemberService.list_pending_invites(request.actor)

        return response_data(
            success=True,
            data={"invites": PendingInviteSerializer(invites, many=True).data},
        )


class OrganizationInviteCreateAPIView(OrgMemberBaseView):
    """POST /organizations/members/invite   {user_id? | email?, role}"""

    throttle_classes = [OrgInviteCreateThrottle]

    def post(self, request):
        serializer = InviteCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        invite = OrganizationMemberService.invite(
            request.actor,
            user_id=data.get("user_id"),
            email=data.get("email"),
            role=data.get("role", ""),
        )

        return response_data(
            success=True,
            message="Invite sent",
            data={"invite": PendingInviteSerializer(invite).data},
            status_code=201,
        )


class OrganizationInviteCancelAPIView(OrgMemberBaseView):
    """POST /organizations/members/invites/<uuid>/cancel"""

    def post(self, request, invite_id):
        OrganizationMemberService.cancel_invite(request.actor, invite_id)
        return response_data(success=True, message="Invite cancelled")


class OrganizationMemberRoleAPIView(OrgMemberBaseView):
    """POST /organizations/members/<uuid>/role   {role}"""

    def post(self, request, member_id):
        serializer = RoleChangeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        member = OrganizationMemberService.change_role(
            request.actor, member_id, serializer.validated_data.get("role", "")
        )

        return response_data(
            success=True,
            message="Role updated",
            data={
                "member": OrganizationMemberSerializer(
                    member, context={"user_id": request.user.id}
                ).data
            },
        )


class OrganizationMemberRemoveAPIView(OrgMemberBaseView):
    """POST /organizations/members/<uuid>/remove"""

    def post(self, request, member_id):
        OrganizationMemberService.remove_member(request.actor, member_id)
        return response_data(success=True, message="Member removed")


class OrganizationLeaveAPIView(OrgMemberBaseView):
    """POST /organizations/members/leave"""

    def post(self, request):
        OrganizationMemberService.leave(request.actor)
        return response_data(success=True, message="You left the organization")


class MyInvitesAPIView(OrgMemberBaseView):
    """GET /organizations/invites/mine"""

    def get(self, request):
        user = _require_user_actor(request)

        return response_data(
            success=True,
            data={
                "invites": MyInviteSerializer(
                    OrganizationMemberService.my_invites(user), many=True
                ).data
            },
        )


class InviteAcceptAPIView(OrgMemberBaseView):
    """POST /organizations/invites/<uuid>/accept"""

    def post(self, request, invite_id):
        user = _require_user_actor(request)

        organization, member = OrganizationMemberService.accept_invite(user, invite_id)

        return response_data(
            success=True,
            message="Invite accepted",
            data={"organization": _organization_payload(organization, member)},
        )


class InviteDeclineAPIView(OrgMemberBaseView):
    """POST /organizations/invites/<uuid>/decline"""

    def post(self, request, invite_id):
        user = _require_user_actor(request)

        OrganizationMemberService.decline_invite(user, invite_id)

        return response_data(success=True, message="Invite declined")


class InviteTokenPageAPIView(OrgMemberErrorsMixin, PublicAPIView):
    """
    GET /organizations/invites/token/<token> — anonymous.

    What the email link's page shows, or one 404 for an unknown, expired, used or
    cancelled link. THE TOKEN IS A CREDENTIAL: nothing here logs it.
    """

    throttle_classes = [OrgInviteTokenThrottle]

    def get(self, request, token):
        return response_data(
            success=True,
            data=OrganizationMemberService.invite_page(token),
        )


class InviteTokenAcceptAPIView(OrgMemberBaseView):
    """POST /organizations/invites/token/<token>/accept"""

    throttle_classes = [OrgInviteTokenThrottle]

    def post(self, request, token):
        organization, member = OrganizationMemberService.accept_invite_by_token(
            request.user, token
        )

        return response_data(
            success=True,
            message="Invite accepted",
            data={"organization": _organization_payload(organization, member)},
        )


class InviteTokenDeclineAPIView(OrgMemberBaseView):
    """POST /organizations/invites/token/<token>/decline"""

    throttle_classes = [OrgInviteTokenThrottle]

    def post(self, request, token):
        OrganizationMemberService.decline_invite_by_token(request.user, token)
        return response_data(success=True, message="Invite declined")
