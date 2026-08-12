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
                    "comment",
                    "recruitment",
                    "career_entry"
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

            # Scoped to the requesting actor throughout: a person holding two
            # clubs must never mark the other one's rows, and the group update
            # below would otherwise reach every recipient in the group.
            owned = Notification.objects.filter(
                recipient_user=actor.user if actor.is_user else None,
                recipient_org=actor.organization if actor.is_org else None
            )

            notification = owned.filter(id=notification_id).first()

            if not notification:
                return response_data(
                    False,
                    "Notification not saved",
                    status_code=400
                )

            # The list renders a group's is_read as all(...), so marking only the
            # primary row leaves a 5-like group looking unread forever.
            if notification.group_key:
                owned.filter(group_key=notification.group_key).update(is_read=True)
            else:
                owned.filter(id=notification.id).update(is_read=True)

            return response_data(
                True,
                "Notification marked as read"
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
        

