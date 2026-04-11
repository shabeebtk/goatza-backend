from django.urls import path
from notifications.views.notification_views import (
    NotificationListAPIView, MarkNotificationReadAPIView, NotificationUnreadCountAPIView, 
    MarkAllNotificationsReadAPIView
)
from notifications.views.user_fcm_views import SaveFCMTokenAPIView, DisableFCMTokenAPIView
# base endpoint '/notifications/

urlpatterns = [
    path('list', NotificationListAPIView.as_view()),
    path('unread/count', NotificationUnreadCountAPIView.as_view()),
    path('mark/read', MarkNotificationReadAPIView.as_view()),
    path('mark/read/all', MarkAllNotificationsReadAPIView.as_view()), 

    path('save/user/fcm/token', SaveFCMTokenAPIView.as_view()), 
    path('disable/user/fcm/token', DisableFCMTokenAPIView.as_view()), 
]
