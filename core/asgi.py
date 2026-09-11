import os

#  SET THIS FIRST 
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")

#  load Django
from django.core.asgi import get_asgi_application

django_asgi_app = get_asgi_application()

# AFTER Django is ready → import channels 
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.security.websocket import OriginValidator
from django.conf import settings

from core.middlewares.jwt_protocol_auth import JWTProtocolAuthMiddleware
from apps.messaging import routing as messaging_routing

application = ProtocolTypeRouter({
    "http": django_asgi_app,

    # OriginValidator is the OUTERMOST layer on purpose: a connection from an
    # origin we do not serve is closed before the JWT is even parsed.
    #
    # WHY IT IS NEEDED AT ALL: the websocket handshake is not subject to the
    # same-origin policy and CORS does not apply to it, so without this any
    # page on the internet can open a socket to this server in a visitor's
    # browser. The JWT lives in a subprotocol rather than a cookie, so this is
    # not classic CSWSH — an attacker page cannot read our token — but the
    # handshake still reaches the middleware and the consumer, and "only
    # authenticated sockets get through" is a much weaker statement than "only
    # our own pages may knock".
    #
    # The allow-list is settings.WEBSOCKET_ALLOWED_ORIGINS, which defaults to
    # CORS_ALLOWED_ORIGINS — see the block above it in core/settings.py for why
    # that, and not AllowedHostsOriginValidator/ALLOWED_HOSTS.
    "websocket": OriginValidator(
        JWTProtocolAuthMiddleware(
            URLRouter(
                messaging_routing.websocket_urlpatterns
            )
        ),
        settings.WEBSOCKET_ALLOWED_ORIGINS,
    ),
})
