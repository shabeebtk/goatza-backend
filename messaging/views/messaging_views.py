from core.views.base_views import BaseAPIView
from rest_framework import status

from messaging.models import Message, ConversationParticipant
from messaging.serializers.message_serializers import MessageSerializer
from messaging.pagination import MessageCursorPagination
from utils.response import response_data 


class MessageListAPIView(BaseAPIView):

    def get(self, request):
        try:
            actor = request.actor
            conversation_id = request.query_params.get("conversation_id")

            # ----------------------------------------
            # VALIDATION
            # ----------------------------------------
            if not conversation_id:
                return response_data(
                    False,
                    "conversation_id required",
                    status_code=status.HTTP_400_BAD_REQUEST
                )

            # ----------------------------------------
            # SECURITY CHECK
            # ----------------------------------------
            is_allowed = ConversationParticipant.objects.filter(
                conversation_id=conversation_id,
                user=actor.user if actor.is_user else None,
                org=actor.organization if actor.is_org else None
            ).exists()

            if not is_allowed:
                return response_data(
                    False,
                    "Not allowed",
                    status_code=status.HTTP_403_FORBIDDEN
                )

            # ----------------------------------------
            # QUERYSET
            # ----------------------------------------
            queryset = (
                Message.objects
                .filter(conversation_id=conversation_id, is_deleted=False)
                .select_related("sender_user__profile", "sender_org__profile")
                .order_by("-created_at")
            )

            # ----------------------------------------
            # PAGINATION
            # ----------------------------------------
            paginator = MessageCursorPagination()
            page = paginator.paginate_queryset(queryset, request)

            serializer = MessageSerializer(page, many=True)

            return response_data(
                True,
                "Messages fetched",
                data={
                    "next_cursor": paginator.get_next_link(),
                    "results": serializer.data
                }
            )

        except Exception as e:
            return response_data(
                False,
                "Something went wrong",
                status_code=500,
                error=str(e)
            )