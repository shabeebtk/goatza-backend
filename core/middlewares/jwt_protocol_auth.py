
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

                    # SUSPENDED ACCOUNT — the websocket half of the check
                    # SimpleJWT already does over HTTP (CHECK_USER_IS_ACTIVE).
                    # Without it a suspended user keeps a live chat socket for
                    # as long as the connection stays open, which is the one
                    # place a suspension would not be felt immediately. Raising
                    # lands in the except below: user and actor stay None and
                    # the consumer closes the connection.
                    if not user.is_active:
                        raise ValueError("inactive user")

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

                        # Membership AND not suspended — mirrors
                        # core.actor.resolve_actor, so a club cannot be spoken
                        # for over the socket after it is suspended over HTTP.
                        if membership and not membership.organization.is_suspended:
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