
from rest_framework_simplejwt.tokens import AccessToken
from django.contrib.auth import get_user_model
from channels.db import database_sync_to_async

User = get_user_model()


class JWTProtocolAuthMiddleware:
    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        scope["user"] = None  # default

        try:
            subprotocols = scope.get("subprotocols", [])

            if len(subprotocols) >= 2:
                protocol_name = subprotocols[0]
                token = subprotocols[1]

                if protocol_name == "access_token":
                    access_token = AccessToken(token)

                    user = await database_sync_to_async(User.objects.get)(
                        id=access_token["user_id"]
                    )

                    scope["user"] = user

        except Exception as e:
            print("JWT WS AUTH ERROR:", str(e))
            scope["user"] = None

        return await self.inner(scope, receive, send)