from django.db import transaction
from django.db.models import Q, Count

from messaging.models import Conversation, ConversationParticipant
from connections.services.follow_services import FollowService
from django.utils import timezone

class ConversationService:

    # ----------------------------------------
    # MAIN ENTRY
    # ----------------------------------------
    @staticmethod
    def get_or_create_conversation(
        actor_user=None,
        actor_org=None,
        target_user=None,
        target_org=None
    ):
        """
        Entry point:
        - prevents duplicate conversations
        - applies follow/request logic
        """

        # ----------------------------------------
        # FIND EXISTING
        # ----------------------------------------
        conversation = ConversationService._get_existing_conversation(
            actor_user, actor_org, target_user, target_org
        )

        if conversation:
            return conversation, False

        # CREATE NEW
        return ConversationService._create_conversation(
            actor_user, actor_org, target_user, target_org
        ), True

    # FIND EXISTING 
    
    @staticmethod
    def _get_existing_conversation(actor_user, actor_org, target_user, target_org):

        queryset = Conversation.objects.filter(
            type=Conversation.Type.DIRECT
        ).annotate(
            participant_count=Count("participants")
        ).filter(
            participant_count=2
        )

        if actor_user and target_user:
            queryset = queryset.filter(
                participants__user=actor_user
            ).filter(
                participants__user=target_user
            )

        elif actor_user and target_org:
            queryset = queryset.filter(
                participants__user=actor_user
            ).filter(
                participants__org=target_org
            )

        elif actor_org and target_user:
            queryset = queryset.filter(
                participants__org=actor_org
            ).filter(
                participants__user=target_user
            )

        elif actor_org and target_org:
            queryset = queryset.filter(
                participants__org=actor_org
            ).filter(
                participants__org=target_org
            )

        return queryset.distinct().first()

    # CREATE CONVERSATION
    @staticmethod
    def _create_conversation(actor_user, actor_org, target_user, target_org):

        with transaction.atomic():

            # CHECK RELATIONSHIP
            is_active = FollowService.is_mutual_follow(
                actor_user, actor_org, target_user, target_org
            )

            status = (
                Conversation.Status.ACTIVE
                if is_active
                else Conversation.Status.REQUESTED
            )

            # CREATE CONVERSATION
            conversation = Conversation.objects.create(
                type=Conversation.Type.DIRECT,
                status=status,
                created_by_user=actor_user if actor_user else None,
                created_by_org=actor_org if actor_org else None,
            )

            # ADD PARTICIPANTS
            ConversationService._create_participants(
                conversation,
                actor_user,
                actor_org,
                target_user,
                target_org,
                is_active
            )

            conversation.refresh_from_db()
            count = ConversationParticipant.objects.filter(
                conversation=conversation
            ).count()

            if conversation.type == Conversation.Type.DIRECT and count != 2:
                raise Exception("Invalid direct conversation setup")

            return conversation

    # PARTICIPANTS
    @staticmethod
    def _create_participants(
        conversation,
        actor_user,
        actor_org,
        target_user,
        target_org,
        is_active
    ):
        participants = []

        # sender
        participants.append(
            ConversationParticipant(
                conversation=conversation,
                user=actor_user,
                org=actor_org,
                has_accepted=True
            )
        )

        # receiver
        participants.append(
            ConversationParticipant(
                conversation=conversation,
                user=target_user,
                org=target_org,
                has_accepted=is_active
            )
        )

        # CHECK
        if conversation.type == Conversation.Type.DIRECT:
            if len(participants) != 2:
                raise Exception("Direct conversation must have exactly 2 participants")

        ConversationParticipant.objects.bulk_create(participants)


    @staticmethod
    def accept_conversation(conversation, user):
        participant = ConversationParticipant.objects.filter(
            conversation=conversation,
            user=user
        ).first()

        if not participant:
            raise Exception("Not a participant")

        participant.has_accepted = True
        participant.save(update_fields=["has_accepted"])

        # activate conversation
        conversation.status = Conversation.Status.ACTIVE
        conversation.save(update_fields=["status"])

        return True


    @staticmethod
    def mark_as_read(conversation, user=None, org=None):
        query = ConversationParticipant.objects.filter(
            conversation=conversation
        )

        if user:
            query = query.filter(user=user)
        elif org:
            query = query.filter(org=org)
        else:
            raise Exception("Invalid actor")

        updated = query.update(
            last_read_at=timezone.now()
        )

        if not updated:
            raise Exception("Participant not found")

        return True

