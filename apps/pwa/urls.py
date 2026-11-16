from django.urls import path

from apps.pwa.views.app_event_views import AppEventsAPIView

# base url - /app/

urlpatterns = [
    path("events", AppEventsAPIView.as_view()),
]
