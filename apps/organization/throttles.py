"""Throttles for organization invites. Rates live in DEFAULT_THROTTLE_RATES."""

from rest_framework.throttling import SimpleRateThrottle

from apps.messaging.throttles import ActorScopedThrottle


class OrgInviteCreateThrottle(ActorScopedThrottle):
    """
    30/day on ``POST /organizations/members/invite``, per org actor.

    Every invite can mail an address the caller typed, which makes this worth
    more to a spammer than most of what an org account does. Per actor because a
    person with two clubs is running two separate rosters.
    """

    scope = "org_invites"


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
