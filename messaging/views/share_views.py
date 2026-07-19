from rest_framework import status

from core.views.base_views import BaseAPIView
from messaging.serializers.share_serializers import ShareRequestSerializer
from messaging.services.exceptions import ContentUnavailableError
from messaging.services.share_service import ShareService
from messaging.throttles import ShareThrottle
from utils.response import response_data


class ShareToConversationsAPIView(BaseAPIView):
    """
    POST /conversations/share

    Share a post or recruitment into existing conversations and/or with people
    who have no conversation yet.

        {
          "target": {"type": "post" | "recruitment", "id": "<uuid>"},
          "conversation_ids": ["<uuid>", ...],          # optional, max 10
          "recipients": [{"actor_type": "user" | "organization",
                          "actor_id": "<uuid>"}],       # optional, max 10
          "note": "check this out"                      # optional caption
        }

    Targets are independent — the response reports each one:

        {"sent": ["<conversation_id>"], "failed": [{"id": …, "reason": …}]}

    Reasons are the machine codes on messaging.services.exceptions, plus
    "conversation_not_found".
    """

    throttle_classes = [ShareThrottle]

    def post(self, request):
        try:
            actor = request.actor

            serializer = ShareRequestSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            payload = serializer.validated_data

            try:
                result = ShareService.share(
                    actor=actor,
                    target_type=payload["target"]["type"],
                    target_id=payload["target"]["id"],
                    conversation_ids=payload["conversation_ids"],
                    recipients=payload["recipients"],
                    note=payload["note"],
                )
            except ContentUnavailableError as exc:
                # The shared object itself is gone or invisible to the sender —
                # nothing to fan out, so this fails the whole request rather
                # than every target individually.
                return response_data(
                    False,
                    str(exc),
                    data={"reason": exc.reason},
                    status_code=status.HTTP_404_NOT_FOUND,
                )

            # Partial success is still a success: some targets may have failed
            # while others were delivered. 200 with both lists.
            return response_data(
                True,
                "Share processed",
                data=result,
            )

        except Exception as e:
            # DRF's own exceptions (validation, throttling, permission) must
            # keep their structured responses and status codes — re-raise them
            # for the exception handler instead of flattening to a 500.
            if hasattr(e, "get_full_details"):
                raise

            return response_data(
                False,
                "Something went wrong",
                status_code=500,
                error=str(e),
            )
