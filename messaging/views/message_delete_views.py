from rest_framework import status

from core.views.base_views import BaseAPIView
from messaging.models import Conversation
from messaging.services.exceptions import MessageError
from messaging.services.message_service import MessageService
from utils.response import response_data


class DeleteMessageAPIView(BaseAPIView):
    """
    DELETE /conversations/<conversation_id>/messages/<message_id>

    Unsend a message for everyone in the thread. Only the actor that SENT the
    message may delete it — being a participant is not enough.

    Soft delete (``is_deleted=True``): every read path already filters deleted
    messages out, and the conversation's last-message pointer is moved back to
    the newest surviving message. Participants are told over the chat socket
    ("message_deleted"), so open windows drop it immediately.
    """

    def delete(self, request, conversation_id, message_id):
        try:
            actor = request.actor

            conversation = Conversation.objects.filter(
                id=conversation_id
            ).first()
            if not conversation:
                return response_data(
                    False,
                    "Conversation not found",
                    status_code=status.HTTP_404_NOT_FOUND,
                )

            try:
                MessageService.delete_message(
                    conversation=conversation,
                    message_id=message_id,
                    actor_user=actor.user if actor.is_user else None,
                    actor_org=actor.organization if actor.is_org else None,
                )
            except MessageError as exc:
                if exc.reason == "message_not_found":
                    code = status.HTTP_404_NOT_FOUND
                elif exc.reason == "not_message_sender":
                    code = status.HTTP_403_FORBIDDEN
                else:
                    code = status.HTTP_400_BAD_REQUEST
                return response_data(
                    False,
                    str(exc),
                    data={"reason": exc.reason},
                    status_code=code,
                )

            return response_data(
                True,
                "Message deleted",
                data={"id": str(message_id)},
                status_code=status.HTTP_200_OK,
            )

        except Exception as e:
            # Let DRF's own exceptions keep their structured responses.
            if hasattr(e, "get_full_details"):
                raise
            return response_data(
                False,
                "Something went wrong",
                data={"detail": str(e)},
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
