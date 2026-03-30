from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from core.mixins.actor_mixin import ActorMixin

'''
handles user and organization - request.actor
'''
class BaseAPIView(ActorMixin, APIView):
    permission_classes = [IsAuthenticated]

    @property
    def actor(self):
        return self.request.actor