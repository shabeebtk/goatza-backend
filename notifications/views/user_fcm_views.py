import logging
from rest_framework import status
from notifications.models import Notification, UserFCMToken
from notifications.pagination import NotificationCursorPagination
from notifications.services.grouping_service import NotificationGroupingService
from utils.response import response_data 
from core.views.base_views import BaseAPIView  

class SaveFCMTokenAPIView(BaseAPIView):

    def post(self, request):
        user = request.user
        token = request.data.get("token")
        device_type = request.data.get("device_type")
        device_name = request.data.get("device_name")

        if not token:
            return response_data(False, "Token required", status_code=400)

        UserFCMToken.objects.update_or_create(
            token=token,
            defaults={
                "user": user,
                "device_type": device_type,
                "device_name": device_name,
                "is_active": True
            }
        )

        return response_data(True, "Token saved")
    

class DisableFCMTokenAPIView(BaseAPIView):
    def post(self, request):
        token = request.data.get("token")

        notification = UserFCMToken.objects.filter(token=token, user=request.actor.user)
        
        if notification:
            notification.update(is_active=False) 

        return response_data(True, "Token disabled")