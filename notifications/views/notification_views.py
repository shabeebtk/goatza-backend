import logging
from rest_framework import status
from notifications.models import Notification
from notifications.pagination import NotificationCursorPagination
from notifications.services.grouping_service import NotificationGroupingService
from utils.response import response_data 
from core.views.base_views import BaseAPIView  

logger = logging.getLogger(__name__)


class NotificationListAPIView(BaseAPIView):

    def get(self, request):
        TAG = "NotificationListAPIView"

        try:
            actor = request.actor

            # ----------------------------------------
            # QUERYSET
            # ----------------------------------------
            queryset = (
                Notification.objects
                .filter(
                    is_deleted=False,
                    recipient_user=actor.user if actor.is_user else None,
                    recipient_org=actor.organization if actor.is_org else None,
                )
                .select_related(
                    "actor_user__profile",
                    "actor_org__profile",
                    "actor_org",
                    "post",
                    "comment"
                )
                .order_by("-created_at")
            )

            # ----------------------------------------
            # PAGINATION
            # ----------------------------------------
            paginator = NotificationCursorPagination()
            paginated_qs = paginator.paginate_queryset(queryset, request)

            # ----------------------------------------
            # GROUPING
            # ----------------------------------------
            grouped_data = NotificationGroupingService.group_notifications(
                paginated_qs
            )

            # ----------------------------------------
            # RESPONSE
            # ----------------------------------------
            return response_data(
                success=True,
                message="Notifications fetched successfully",
                data={
                    "next_cursor": paginator.get_next_cursor(),
                    "results": grouped_data
                }
            )

        except Exception as e:
            logger.error(f"{TAG} | Error | {str(e)}")

            return response_data(
                success=False,
                message="Something went wrong",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                error=str(e)
            )
        


class MarkNotificationReadAPIView(BaseAPIView):

    def post(self, request):
        TAG = "MarkNotificationReadAPIView"

        try:
            actor = request.actor
            notification_id = request.query_params.get('notification_id')

            is_saved = Notification.objects.filter(
                id=notification_id,
                recipient_user=actor.user if actor.is_user else None,
                recipient_org=actor.organization if actor.is_org else None
            ).update(is_read=True)


            return response_data(
                True if is_saved else False,
                "Notification marked as read" if is_saved else "Notification not saved",
                status_code=200 if is_saved else 400
            )

        except Exception as e:
            logger.error(f"{TAG} | Error | {str(e)}")

            return response_data(
                False,
                "Something went wrong",
                status_code=500,
                error=str(e)
            )
        



class MarkAllNotificationsReadAPIView(BaseAPIView):

    def post(self, request):
        TAG = "MarkAllNotificationsReadAPIView"

        try:
            actor = request.actor

            Notification.objects.filter(
                recipient_user=actor.user if actor.is_user else None,
                recipient_org=actor.organization if actor.is_org else None,
                is_read=False
            ).update(is_read=True)

            return response_data(
                True,
                "All notifications marked as read"
            )

        except Exception as e:
            logger.error(f"{TAG} | Error | {str(e)}")

            return response_data(
                False,
                "Something went wrong",
                status_code=500,
                error=str(e)
            )
        

class NotificationUnreadCountAPIView(BaseAPIView):

    def get(self, request):
        TAG = "NotificationUnreadCountAPIView"

        try:
            actor = request.actor

            count = Notification.objects.filter(
                recipient_user=actor.user if actor.is_user else None,
                recipient_org=actor.organization if actor.is_org else None,
                is_read=False
            ).count()

            return response_data(
                True,
                "Unread count fetched",
                data={"count": count}
            )

        except Exception as e:
            logger.error(f"{TAG} | Error | {str(e)}")

            return response_data(
                False,
                "Something went wrong",
                status_code=500,
                error=str(e)
            )
        

