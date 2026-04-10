from django.urls import path
from notifications.views.notification_views import (
    NotificationListAPIView, MarkNotificationReadAPIView, NotificationUnreadCountAPIView, 
    MarkAllNotificationsReadAPIView
)
# base endpoint '/notifications/

urlpatterns = [
    path('list', NotificationListAPIView.as_view()),
    path('unread/count', NotificationUnreadCountAPIView.as_view()),
    path('mark/read', MarkNotificationReadAPIView.as_view()),
    path('mark/read/all', MarkAllNotificationsReadAPIView.as_view())
]
