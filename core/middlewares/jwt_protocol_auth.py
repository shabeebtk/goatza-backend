
from rest_framework_simplejwt.tokens import AccessToken
from django.contrib.auth import get_user_model
from channels.db import database_sync_to_async
from core.actor import Actor
from urllib.parse import parse_qs
from organization.models import OrganizationMember

User = get_user_model()

class JWTProtocolAuthMiddleware:
    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        scope["user"] = None
        scope["actor"] = None   # for managing ORG or User

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

                    # RESOLVE ACTOR 
                    # ----------------------------------------
                    query_string = scope.get("query_string", b"").decode()
                    query_params = parse_qs(query_string)

                    actor_type = query_params.get("actor_type", ["user"])[0]
                    org_id = query_params.get("org_id", [None])[0]

                    if actor_type == "organization" and org_id:
                        membership = await database_sync_to_async(
                            OrganizationMember.objects.select_related("organization").filter(
                                user=user,
                                organization_id=org_id
                            ).first
                        )()

                        if membership:
                            scope["actor"] = Actor(
                                actor_type="organization",
                                organization=membership.organization
                            )
                        else:
                            scope["actor"] = None
                    else:
                        scope["actor"] = Actor(
                            actor_type="user",
                            user=user
                        )

        except Exception as e:
            print("JWT WS AUTH ERROR:", str(e))
            scope["user"] = None
            scope["actor"] = None

        return await self.inner(scope, receive, send)