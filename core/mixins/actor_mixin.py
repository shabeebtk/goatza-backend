from core.actor import resolve_actor


class ActorMixin:
    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)

        # JWT already processed here
        request.actor = resolve_actor(request)