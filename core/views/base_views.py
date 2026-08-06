from rest_framework.views import APIView
from rest_framework.permissions import AllowAny, IsAuthenticated
from core.mixins.actor_mixin import ActorMixin
from core.throttles import PublicReadThrottle

'''
handles user and organization - request.actor
'''
class BaseAPIView(ActorMixin, APIView):
    permission_classes = [IsAuthenticated]

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