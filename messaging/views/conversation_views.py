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
from organization.services.user_organization_services import UserOrganizationService
from core.constant import TYPE_USER
from organization.models import Organization
from accounts.services.user_services import UserService
from organization.services.organization_service import OrganizationService



class GetOrCreateConversationAPIView(BaseAPIView):

    def post(self, request):
        try:
            actor = request.actor
            username = request.data.get("username")

            if not username:
                return response_data(
                    False,
                    "username required",
                    status_code=status.HTTP_400_BAD_REQUEST
                )

            # RESOLVE TARGET (USER OR ORG)
            try:
                target = UserOrganizationService.get_user_or_org_by_username(username)
            except ValueError:
                return response_data(
                    False,
                    "Profile not found",
                    status_code=status.HTTP_404_NOT_FOUND
                )

            target_user = None
            target_org = None

            if target["type"] == TYPE_USER:
                target_user = UserService.get_user_by_id(target['id'])
            else:
                target_org = OrganizationService.get_organization(id=target['id'])

            # PREVENT SELF CHAT
            if actor.is_user and target_user and actor.user.id == target_user.id:
                return response_data(False, "Cannot chat with yourself", status_code=400)

            if actor.is_org and target_org and actor.organization.id == target_org.id:
                return response_data(False, "Cannot chat with same organization", status_code=400)

            # CREATE CONVERSATION 
            conversation, created = ConversationService.get_or_create_conversation(
                actor_user=actor.user if actor.is_user else None,
                actor_org=actor.organization if actor.is_org else None,
                target_user=target_user,
                target_org=target_org
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

            # FILTER PARAM
            filter_type = request.query_params.get("type", "all")
            search = request.query_params.get("search", "").strip()

            queryset = Conversation.objects.filter(
                participants__user=actor.user if actor.is_user else None,
                participants__org=actor.organization if actor.is_org else None
            )

            # ----------------------------------------
            # FILTER LOGIC
            # ----------------------------------------
            if filter_type == "requested":
                queryset = queryset.filter(
                    participants__user=actor.user if actor.is_user else None,
                    participants__org=actor.organization if actor.is_org else None,
                    participants__has_accepted=False
                )

            elif filter_type == "active":
                queryset = queryset.filter(
                    participants__user=actor.user if actor.is_user else None,
                    participants__org=actor.organization if actor.is_org else None,
                    participants__has_accepted=True
                )

            
            # SEARCH
            if search:
                queryset = queryset.filter(
                    Q(participants__user__username__icontains=search) |
                    Q(participants__user__profile__name__icontains=search) |
                    Q(participants__org__username__icontains=search) |
                    Q(participants__org__name__icontains=search)
                )


            # ----------------------------------------
            # OPTIMIZATION
            # ----------------------------------------
            queryset = (
                queryset
                .select_related("last_message")
                .prefetch_related(
                    "participants__user__profile",
                    "participants__org__profile"
                )
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
                conversation = ConversationService.get_conversation(id=conversation_id)
            except Conversation.DoesNotExist:
                return response_data(
                    False,
                    "Conversation not found",
                    status_code=status.HTTP_404_NOT_FOUND
                )

            # SECURITY CHECK
            is_participant = conversation.participants.filter(
                user=actor.user if actor.is_user else None,
                org=actor.organization if actor.is_org else None
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
                user=actor.user if actor.is_user else None,
                org=actor.organization if actor.is_org else None
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

            # FETCH
            try:
                conversation = (
                    Conversation.objects
                    .select_related("last_message")
                    .prefetch_related(
                        "participants__user__profile", "participants__org__profile"
                    )
                    .get(id=conversation_id)
                )
            except Conversation.DoesNotExist:
                return response_data(
                    False,
                    "Conversation not found",
                    status_code=status.HTTP_404_NOT_FOUND
                )

            # CHECK PARTICIPANT
            is_participant = ConversationService.is_participants(
                conversation=conversation,
                actor=actor
            )
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
            is_partcipant = ConversationService.is_participants(
                conversation=conversation,
                actor=actor
            )
            if not is_partcipant:
                return response_data(False, "Access denied", status_code=403)

            ConversationService.accept_conversation(
                conversation,
                actor_user=actor.user if actor.is_user else None,
                actor_org=actor.organization if actor.is_org else None,
            )

            return response_data(True, "Conversation accepted")

        except Exception as e:
            return response_data(False, "Error", status_code=500, error=str(e))




