from rest_framework.views import APIView
from rest_framework.permissions import AllowAny, IsAuthenticated
from core.mixins.actor_mixin import ActorMixin
from core.throttles import PublicReadThrottle
from legal.permissions import HasAcceptedCurrentTerms

'''
handles user and organization - request.actor
'''
class BaseAPIView(ActorMixin, APIView):
    # HasAcceptedCurrentTerms is HERE, not only in DEFAULT_PERMISSION_CLASSES:
    # a class attribute REPLACES the setting, and almost every view in the
    # app reaches DRF through this class. Left to the setting alone, the gate
    # would apply to nothing that posts a post, sends a message, applies to a
    # recruitment or logs a match.
    #
    # Safe to inherit blindly: reads pass, anonymous callers pass, and every
    # recovery route is exempt by PATH (legal.permissions.EXEMPT_PATHS), so a
    # subclass can never lock a user out of clearing the gate.
    permission_classes = [IsAuthenticated, HasAcceptedCurrentTerms]

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