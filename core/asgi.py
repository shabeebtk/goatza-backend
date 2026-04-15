import os

#  SET THIS FIRST 
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")

#  load Django
from django.core.asgi import get_asgi_application

django_asgi_app = get_asgi_application()

# AFTER Django is ready → import channels 
from channels.routing import ProtocolTypeRouter, URLRouter
from core.middlewares.jwt_protocol_auth import JWTProtocolAuthMiddleware
import messaging.routing

application = ProtocolTypeRouter({
    "http": django_asgi_app,

    "websocket": JWTProtocolAuthMiddleware(
        URLRouter(
            messaging.routing.websocket_urlpatterns
        )
    ),
})