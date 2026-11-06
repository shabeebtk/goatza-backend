"""Throttles for organization invites. Rates live in DEFAULT_THROTTLE_RATES."""

from rest_framework.throttling import SimpleRateThrottle

from apps.messaging.throttles import ActorScopedThrottle
from apps.organization.models import OrganizationMember
from apps.organization.permissions import OrgPermission, has_org_permission
from utils.validations import is_valid_uuid


class OrgInviteCreateThrottle(ActorScopedThrottle):
    """
    30/day on ``POST /organizations/members/invite``, per organization.

    Every invite can mail an address the caller typed, which makes this worth
    more to a spammer than most of what an org account does. Per org, not per
    member, or an org could send 30 a day for each person in it.
    """

    scope = "org_invites"

    def org_ident(self, user, org_id):
        """
        The org's shared bucket, for those who can actually invite. A request is
        counted whether or not it goes on to be refused, and the header is not even
        verified yet (see ActorScopedThrottle), so anybody else drawing from it
        could spend the club's daily allowance: an outsider naming the org, or a
        coach whose invites all answer 403. They get a bucket of their own.
        """
        member = (
            OrganizationMember.objects
            .filter(organization_id=org_id, user=user)
            .first()
            if is_valid_uuid(org_id)
            else None
        )

        if has_org_permission(member, OrgPermission.MEMBERS_MANAGE):
            return f"org:{org_id}"

        return super().org_ident(user, org_id)


class OrgInviteTokenThrottle(SimpleRateThrottle):
    """
    30/min per IP on everything that takes an invite token.

    A GUESSING limit, like guardians' PublicConsentReadThrottle, but keyed on the
    IP for signed-in callers too: an AnonRateThrottle exempts them, and accepting
    a token is exactly what a signed-in caller does. DRF's ``get_ident`` is what
    honours NUM_PROXIES behind Render.
    """

    scope = "org_invite_token"

    def get_cache_key(self, request, view):
        return self.cache_format % {
            "scope": self.scope,
            "ident": self.get_ident(request),
        }
