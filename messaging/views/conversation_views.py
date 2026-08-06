from rest_framework.views import APIView
from django.db.models import Q, Exists, OuterRef, Case, When, BooleanField
from rest_framework import status
from core.views.base_views import BaseAPIView
from messaging.models import Conversation, ConversationParticipant, Message
from messaging.selectors.message_selectors import SHARED_SELECT_RELATED
from messaging.selectors.share_selectors import ShareViewer
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


# The conversation list and detail both render `last_message` through
# MessageSerializer, so they need the same joins a page of messages needs — one
# level deeper. Derived from MessageSelector's list rather than restated, so a
# new shared type can never be joined in one place and missed in the other.
LAST_MESSAGE_SELECT_RELATED = tuple(
    f"last_message__{path}" for path in SHARED_SELECT_RELATED
)

# The many-sided reads. Not narrowed to is_primary the way MessageSelector
# narrows them: there is exactly one last_message per conversation, so the
# saving would be a row or two per thread and the extra Prefetch objects would
# cost more to keep in sync than they save.
LAST_MESSAGE_PREFETCH = (
    "last_message__shared_post__media",
    "last_message__shared_recruitment__media",
    "last_message__shared_profile_user__sports__sport",
    "last_message__shared_profile_user__positions__position",
    "last_message__shared_profile_org__locations",
)


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



class MessageTargetSearchAPIView(BaseAPIView):
    """
    GET /conversations/search?q=<query>&limit=<n>

    Prioritised people / organization search for the messages screen so the
    actor can start a NEW conversation straight from the search box, not just
    filter existing ones. Priority: people already in the conversation list →
    people the actor follows → everyone else (users + organizations).

    Each result carries ``conversation_id`` when a direct conversation already
    exists (open it directly) or ``null`` (open via the get-or-create flow).
    """

    def get(self, request):
        try:
            actor = request.actor

            # Actor guard — users and organizations only.
            if not actor or (not actor.is_user and not actor.is_org):
                return response_data(
                    False,
                    "Search is only available for users and organizations",
                    status_code=400,
                )

            query = (
                request.query_params.get("q")
                or request.query_params.get("search")
                or ""
            ).strip()

            if not query:
                return response_data(True, "No query", data=[])

            try:
                limit = int(request.query_params.get("limit", 20))
            except (TypeError, ValueError):
                limit = 20

            results = ConversationService.search_message_targets(
                actor, query, limit=limit
            )

            return response_data(True, "Search results", data=results)

        except Exception as e:
            return response_data(
                False,
                "Something went wrong",
                status_code=500,
                error=str(e),
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

            # HIDE EMPTY THREADS
            # A conversation created via get-or-create but never messaged has no
            # last_message — don't surface those empty threads in either the
            # Chats or the Requests list.
            queryset = queryset.filter(last_message__isnull=False)

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
                .select_related(
                    "last_message",
                    # the last message may be a share — join what its preview reads
                    *LAST_MESSAGE_SELECT_RELATED,
                )
                .prefetch_related(
                    "participants__user__profile",
                    "participants__org__profile",
                    *LAST_MESSAGE_PREFETCH,
                )
                .order_by("-last_message_at", "-created_at")
                .distinct()
            )

            serializer = ConversationListSerializer(
                queryset,
                many=True,
                # One viewer for the whole page — see ConversationListSerializer
                # .get_last_message.
                context={"request": request, "viewer": ShareViewer.from_actor(actor)}
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
                    .select_related(
                        "last_message",
                        *LAST_MESSAGE_SELECT_RELATED,
                    )
                    .prefetch_related(
                        "participants__user__profile", "participants__org__profile",
                        *LAST_MESSAGE_PREFETCH,
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
                context={
                    "request": request,
                    "viewer": ShareViewer.from_actor(actor),
                }
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


class ConversationUnreadSummaryAPIView(BaseAPIView):
    """
    Aggregate unread badge counts for the current actor, split into
    accepted chats vs. pending message requests. Powers the nav-bar
    message badge and the Chats / Requests tab badges.

    Returns: {"chats": int, "requests": int, "total": int}
    where each count is the number of conversations that have at least
    one unread message from the other side (not the raw message total).
    """

    def get(self, request):
        try:
            actor = request.actor

            # The actor's own participant rows (user XOR org).
            participants = ConversationParticipant.objects.filter(
                user=actor.user if actor.is_user else None,
                org=actor.organization if actor.is_org else None,
            )

            # Messages in the same conversation from the OTHER side.
            other_messages = Message.objects.filter(
                conversation=OuterRef("conversation"),
                is_deleted=False,
            )
            if actor.is_user:
                other_messages = other_messages.exclude(sender_user=actor.user)
            else:
                other_messages = other_messages.exclude(sender_org=actor.organization)

            # A conversation is unread when it has any such message newer than
            # the participant's last_read_at (or any at all if never read).
            has_unread = Case(
                When(last_read_at__isnull=True, then=Exists(other_messages)),
                default=Exists(
                    other_messages.filter(created_at__gt=OuterRef("last_read_at"))
                ),
                output_field=BooleanField(),
            )

            unread = participants.annotate(has_unread=has_unread).filter(has_unread=True)

            chats = unread.filter(has_accepted=True).count()
            requests = unread.filter(has_accepted=False).count()

            return response_data(
                True,
                "Unread summary fetched",
                data={
                    "chats": chats,
                    "requests": requests,
                    "total": chats + requests,
                },
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




