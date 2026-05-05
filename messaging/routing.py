from django.urls import re_path
from messaging.consumers.chat_consumers import ChatConsumer
from messaging.consumers.notification_consumers import UserNotificationsConsumer

websocket_urlpatterns = [
    re_path(r"ws/chat/(?P<conversation_id>[^/]+)/$", ChatConsumer.as_asgi()),
    re_path(r"ws/notifications/$", UserNotificationsConsumer.as_asgi()),
]