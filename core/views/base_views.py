from rest_framework.views import APIView
from rest_framework.permissions import AllowAny, IsAuthenticated
from core.mixins.actor_mixin import ActorMixin
from core.throttles import PublicReadThrottle
from apps.guardians.permissions import HasGuardianConsentIfMinor
from apps.legal.permissions import HasAcceptedCurrentTerms

'''
handles user and organization - request.actor
'''
class BaseAPIView(ActorMixin, APIView):
    # BOTH GATES ARE HERE, not only in DEFAULT_PERMISSION_CLASSES: a class
    # attribute REPLACES the setting, and almost every view in the app reaches
    # DRF through this class. Left to the setting alone, they would apply to
    # nothing that posts a post, sends a message, applies to a recruitment or
    # logs a match.
    #
    # They travel together everywhere for that reason — see
    # guardians/tests/test_gate.py, which fails if a view ever lists one
    # without the other.
    #
    # Safe to inherit blindly: anonymous callers pass, and every recovery route
    # is exempt by PATH in each gate's own EXEMPT_PATHS, so a subclass can
    # never lock a user out of clearing either one. Note the two rules are NOT
    # the same — the terms gate lets reads through, the guardian gate does not,
    # because an unconsented child being READ is the thing it exists to stop.
    permission_classes = [
        IsAuthenticated, HasAcceptedCurrentTerms, HasGuardianConsentIfMinor
    ]

    @property
    def actor(self):
        return self.request.actor


class PublicAPIView(ActorMixin, APIView):
    """
    Read-only surface reachable without a token.

    ActorMixin still runs, so a logged-in caller hitting a public endpoint is
    resolved as their normal actor (and gets relationship data); an anonymous
    caller gets request.actor = None and the anonymous view of everything.

    Deliberately a sibling of BaseAPIView rather than a subclass with looser
    permissions: BaseAPIView stays IsAuthenticated for every endpoint that
    already inherits it, and the only views that can be reached anonymously are
    the ones that opt in by naming this class.
    """

    permission_classes = [AllowAny]
    throttle_classes = [PublicReadThrottle]

    @property
    def actor(self):
        return self.request.actor