from rest_framework.views import APIView
from django.db.models import Q
from rest_framework import status
from core.views.base_views import BaseAPIView
from messaging.models import Conversation, ConversationParticipant
from messaging.serializers.conversation_serializers import (
     ConversationListSerializer, ConversationDetailSerializer
)
from utils.response import response_data 
from messaging.services.conversation_service import ConversationService
from accounts.services.user_services import UserService
from accounts.serializers.user_serializers import UserMiniSerializer


class GetOrCreateConversationAPIView(BaseAPIView):

    def post(self, request):
        try:
            actor = request.actor
            actor_user = actor.user
            username = request.data.get('username')

            target_user = UserService.get_user_by_username(username)

            if not target_user:
                return response_data(
                    False,
                    "User not found",
                    status_code=status.HTTP_404_NOT_FOUND
                )


            # SERVICE CALL
            conversation, created = ConversationService.get_or_create_conversation(
                actor_user=actor_user,
                target_user=target_user
            )


            # RESPONSE
            return response_data(
                True,
                "Conversation ready",
                data={
                    "conversation_id": str(conversation.id),
                    "status": conversation.status,
                    "is_new": created,
                    "can_message": conversation.status == "active"
                }
            )

        except Exception as e:
            return response_data(
                False,
                "Something went wrong",
                status_code=500,
                error=str(e)
            )



class ConversationListAPIView(BaseAPIView):

    def get(self, request):
        try:
            actor = request.actor

            if not actor.is_user:
                return response_data(
                    False,
                    "Only users supported",
                    status_code=status.HTTP_400_BAD_REQUEST
                )

            user = actor.user

            # FILTER PARAM
            filter_type = request.query_params.get("type", "all")
            search = request.query_params.get("search", "").strip()

            queryset = Conversation.objects.filter(
                participants__user=user
            )

            # ----------------------------------------
            # FILTER LOGIC
            # ----------------------------------------

            if filter_type == "requested":
                queryset = queryset.filter(
                    status=Conversation.Status.REQUESTED,
                    participants__user=user,
                    participants__has_accepted=False
                )

            elif filter_type == "active":
                queryset = queryset.filter(
                    status=Conversation.Status.ACTIVE,
                    participants__user=user,
                    participants__has_accepted=True
                )

            
            # SEARCH
            if search:
                queryset = queryset.filter(
                    Q(participants__user__username__icontains=search) |
                    Q(participants__user__profile__name__icontains=search) 
                )


            # ----------------------------------------
            # OPTIMIZATION
            # ----------------------------------------
            queryset = (
                queryset
                .select_related("last_message")
                .prefetch_related("participants__user__profile")
                .order_by("-last_message_at", "-created_at")
                .distinct()
            )

            serializer = ConversationListSerializer(
                queryset,
                many=True,
                context={"request": request}
            )

            return response_data(
                True,
                data=serializer.data
            )

        except Exception as e:
            return response_data(
                False,
                "Something went wrong",
                status_code=500,
                error=str(e)
            )




class MarkConversationReadAPIView(BaseAPIView):

    def post(self, request):
        try:
            actor = request.actor
            conversation_id = request.data.get("conversation_id")

            if not conversation_id:
                return response_data(
                    False,
                    "conversation_id required",
                    status_code=status.HTTP_400_BAD_REQUEST
                )

            # GET CONVERSATION
            try:
                conversation = Conversation.objects.get(id=conversation_id)
            except Conversation.DoesNotExist:
                return response_data(
                    False,
                    "Conversation not found",
                    status_code=status.HTTP_404_NOT_FOUND
                )

            # SECURITY CHECK
            is_participant = conversation.participants.filter(
                user=actor.user
            ).exists()

            if not is_participant:
                return response_data(
                    False,
                    "Access denied",
                    status_code=status.HTTP_403_FORBIDDEN
                )

            # SERVICE CALL
            ConversationService.mark_as_read(
                conversation=conversation,
                user=actor.user
            )

            return response_data(
                True,
                "Conversation marked as read"
            )

        except Exception as e:
            return response_data(
                False,
                "Something went wrong",
                status_code=500,
                error=str(e)
            )




class ConversationDetailAPIView(BaseAPIView):

    def get(self, request, conversation_id):
        try:
            actor = request.actor

            if not actor.is_user:
                return response_data(
                    False,
                    "Only users supported",
                    status_code=status.HTTP_400_BAD_REQUEST
                )

            user = actor.user

            # FETCH
            try:
                conversation = (
                    Conversation.objects
                    .select_related("last_message")
                    .prefetch_related("participants__user__profile")
                    .get(id=conversation_id)
                )
            except Conversation.DoesNotExist:
                return response_data(
                    False,
                    "Conversation not found",
                    status_code=status.HTTP_404_NOT_FOUND
                )

            # CHECK PARTICIPANT
            is_participant = conversation.participants.filter(
                user=user
            ).exists()

            if not is_participant:
                return response_data(
                    False,
                    "Access denied",
                    status_code=status.HTTP_403_FORBIDDEN
                )

            # SERIALIZE
            serializer = ConversationDetailSerializer(
                conversation,
                context={"request": request}
            )

            return response_data(
                True,
                "Conversation fetched",
                data=serializer.data
            )

        except Exception as e:
            return response_data(
                False,
                "Something went wrong",
                status_code=500,
                error=str(e)
            )


class AcceptConversationAPIView(BaseAPIView):

    def post(self, request):
        try:
            actor = request.actor
            conversation_id = request.data.get("conversation_id")

            conversation = Conversation.objects.get(id=conversation_id)

            # security
            if not conversation.participants.filter(user=actor.user).exists():
                return response_data(False, "Access denied", status_code=403)

            ConversationService.accept_conversation(
                conversation,
                actor.user
            )

            return response_data(True, "Conversation accepted")

        except Exception as e:
            return response_data(False, "Error", status_code=500, error=str(e))




