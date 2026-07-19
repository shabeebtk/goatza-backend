from rest_framework import status

from core.views.base_views import BaseAPIView
from messaging.models import Conversation
from messaging.serializers.media_serializers import (
    SendImageMessageSerializer,
    SendVideoMessageSerializer,
)
from messaging.serializers.message_serializers import MessageSerializer
from messaging.services.exceptions import MessageError
from messaging.services.message_service import MessageService
from messaging.throttles import ChatMediaThrottle
from utils.response import response_data


class SendMediaMessageAPIView(BaseAPIView):
    """
    POST /conversations/<conversation_id>/messages/media

    Send a photo or video into a conversation. The media is already on
    Cloudinary (signed direct upload); the body carries the resulting URL +
    metadata, which the service re-validates before trusting.

        {
          "media_type": "image" | "video",     # defaults to "image"
          "media_url": "https://res.cloudinary.com/<cloud>/.../chat/users/<id>/<uuid>.jpg",
          "media_public_id": "chat/users/<id>/<uuid>",
          "width": 1080, "height": 1350, "size_bytes": 834221,
          "duration_ms": 8200,                  # video only
          "caption": "optional"
        }

    Returns the full serialized message so the client can reconcile its
    optimistic bubble. Throttled per actor (30/min), like the share endpoint.
    """

    throttle_classes = [ChatMediaThrottle]

    def post(self, request, conversation_id):
        try:
            actor = request.actor
            is_video = request.data.get("media_type") == "video"

            serializer_cls = (
                SendVideoMessageSerializer if is_video
                else SendImageMessageSerializer
            )
            serializer = serializer_cls(data=request.data)
            serializer.is_valid(raise_exception=True)
            payload = serializer.validated_data

            conversation = Conversation.objects.filter(
                id=conversation_id
            ).first()
            if not conversation:
                return response_data(
                    False,
                    "Conversation not found",
                    status_code=status.HTTP_404_NOT_FOUND,
                )

            sender_user = actor.user if actor.is_user else None
            sender_org = actor.organization if actor.is_org else None

            try:
                if is_video:
                    message = MessageService.send_video_message(
                        conversation=conversation,
                        sender_user=sender_user,
                        sender_org=sender_org,
                        media_url=payload["media_url"],
                        media_public_id=payload["media_public_id"],
                        width=payload.get("width"),
                        height=payload.get("height"),
                        duration_ms=payload.get("duration_ms"),
                        size_bytes=payload.get("size_bytes"),
                        caption=payload.get("caption", ""),
                    )
                else:
                    message = MessageService.send_image_message(
                        conversation=conversation,
                        sender_user=sender_user,
                        sender_org=sender_org,
                        media_url=payload["media_url"],
                        media_public_id=payload["media_public_id"],
                        width=payload.get("width"),
                        height=payload.get("height"),
                        size_bytes=payload.get("size_bytes"),
                        caption=payload.get("caption", ""),
                    )
            except MessageError as exc:
                # not_a_participant → 403; invalid_media → 400.
                code = (
                    status.HTTP_403_FORBIDDEN
                    if exc.reason == "not_a_participant"
                    else status.HTTP_400_BAD_REQUEST
                )
                return response_data(
                    False,
                    str(exc),
                    data={"reason": exc.reason},
                    status_code=code,
                )

            data = MessageSerializer(message, context={"request": request}).data
            return response_data(
                True,
                "Media sent",
                data=data,
                status_code=status.HTTP_201_CREATED,
            )

        except Exception as e:
            # Let DRF's own exceptions (validation, throttle) keep their
            # structured responses instead of flattening to a 500.
            if hasattr(e, "get_full_details"):
                raise
            return response_data(
                False,
                "Something went wrong",
                status_code=500,
                error=str(e),
            )
