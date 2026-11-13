"""Throttles for squads. Rates live in DEFAULT_THROTTLE_RATES."""

from rest_framework.throttling import SimpleRateThrottle

from apps.messaging.throttles import ActorScopedThrottle
from apps.organization.models import OrganizationMember
from apps.organization.permissions import OrgPermission, has_org_permission
from utils.validations import is_valid_uuid


class SquadDirectInviteThrottle(ActorScopedThrottle):
    """
    60/day on ``POST /squads/<id>/invites``, per organization.

    Every direct invite is a push to a person who did not ask for it, so it is
    worth more to somebody spamming players than most of what a club does. Per
    org, not per member, or a club could send 60 a day for each coach.
    """

    scope = "squad_invites"

    def org_ident(self, user, org_id):
        """
        The org's shared bucket, only for those who can actually invite — the
        same reasoning as OrgInviteCreateThrottle. A request is counted whether
        or not it is then refused, and the actor header is not verified yet, so
        an outsider naming the org, or a member without squads.review, would
        otherwise spend the club's daily allowance. They get a bucket of their own.
        """
        member = (
            OrganizationMember.objects
            .filter(organization_id=org_id, user=user)
            .first()
            if is_valid_uuid(org_id)
            else None
        )

        if has_org_permission(member, OrgPermission.SQUADS_REVIEW):
            return f"org:{org_id}"

        return super().org_ident(user, org_id)


class SquadInviteTokenThrottle(SimpleRateThrottle):
    """
    30/min per IP on everything that takes a squad invite token — the page and
    the join. A copy of OrgInviteTokenThrottle: keyed on the IP for signed-in
    callers too, because the only way to find a link is to try links and a
    session must not buy a way round that. DRF's ``get_ident`` honours
    NUM_PROXIES behind Render.
    """

    scope = "squad_invite_token"

    def get_cache_key(self, request, view):
        return self.cache_format % {
            "scope": self.scope,
            "ident": self.get_ident(request),
        }


class SquadJoinThrottle(SimpleRateThrottle):
    """
    20/hour per PERSON on joining through a link. Every request notifies the
    club's reviewers, so this bounds how much one account can make them hear,
    whichever squads it aims at. Keyed on the user, not the actor header: a
    join is always the person.
    """

    scope = "squad_join"

    def get_cache_key(self, request, view):
        user = request.user
        if user and user.is_authenticated:
            ident = f"user:{user.pk}"
        else:
            ident = self.get_ident(request)
        return self.cache_format % {"scope": self.scope, "ident": ident}
