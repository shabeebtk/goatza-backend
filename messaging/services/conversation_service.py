from django.template.defaultfilters import default
from django.db import transaction, IntegrityError
from django.db.models import Q, Count
from messaging.models import Conversation, ConversationParticipant
from connections.services.follow_services import FollowService
from django.utils import timezone


class ConversationService:

    # ----------------------------------------
    # MAIN ENTRY
    # ----------------------------------------
    @staticmethod
    def get_conversation(id=None):
        return Conversation.objects.get(id=id)
      

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

            # GENERATE UNIVERSAL KEY
            pair_key = ConversationService._generate_pair_key(
                actor_user, actor_org, target_user, target_org
            )

            # LOCK + CHECK
            existing = Conversation.objects.select_for_update().filter(
                direct_pair_key=pair_key
            ).first()

            if existing:
                return existing

            # CHECK RELATIONSHIP
            is_active = FollowService.is_mutual_follow(
                actor_user, actor_org, target_user, target_org
            )

            status = (
                Conversation.Status.ACTIVE
                if is_active
                else Conversation.Status.REQUESTED
            )

            try:
                conversation, created = Conversation.objects.get_or_create(
                    type=Conversation.Type.DIRECT,
                    direct_pair_key=pair_key,
                    defaults={
                        "status": status,
                        "created_by_user": actor_user if actor_user else None,
                        "created_by_org": actor_org if actor_org else None,
                    }
                )
            except IntegrityError:
                # RACE CONDITION SAFE
                return Conversation.objects.get(direct_pair_key=pair_key)

            if created:
                ConversationService._create_participants(
                    conversation,
                    actor_user,
                    actor_org,
                    target_user,
                    target_org,
                    is_active
                )

            return conversation

    @staticmethod
    def _generate_pair_key(actor_user=None, actor_org=None, target_user=None, target_org=None):
        """
        Generates a unique, order-independent key for ANY direct conversation
        """
        def get_actor_key(user, org):
            if user:
                return f"user:{user.id}"
            elif org:
                return f"org:{org.id}"
            return None

        a1 = get_actor_key(actor_user, actor_org)
        a2 = get_actor_key(target_user, target_org)

        if not a1 or not a2:
            raise Exception("Invalid actors for conversation")

        # SORT to make it order independent
        sorted_keys = sorted([a1, a2])

        return f"{sorted_keys[0]}__{sorted_keys[1]}"

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
    def is_participants(coversation, actor):
        return ConversationParticipant.objects.filter(
            coversation=coversation,
            user=actor.user if actor.is_user else None,
            user=actor.organization if actor.is_org else None
        ).exists()


    @staticmethod
    def accept_conversation(conversation, actor_user, actor_org):
        if actor_user:
            participant = ConversationParticipant.objects.filter(
                conversation=conversation,
                user=actor_user
            ).first()
            
        elif actor_org:
            participant = ConversationParticipant.objects.filter(
                conversation=conversation,
                org=actor_org
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

