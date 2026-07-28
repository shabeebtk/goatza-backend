
from rest_framework import serializers
from messaging.models import Conversation, ConversationParticipant
from accounts.serializers.user_serializers import UserMiniSerializer
from shared.serializers.actor_serializers import ActorMiniSerializer

class ConversationListSerializer(serializers.ModelSerializer):

    other_participant = serializers.SerializerMethodField()
    last_message = serializers.SerializerMethodField()
    unread_count = serializers.SerializerMethodField()

    class Meta:
        model = Conversation
        fields = [
            "id",
            "type",
            "status",
            "last_message",
            "last_message_at",
            "other_participant",
            "unread_count",
        ]

    # ----------------------------------------
    # OTHER PARTICIPANT (USER OR ORG)
    # ----------------------------------------
    def get_other_participant(self, obj):
        actor = self.context["request"].actor

        participant = obj.participants.exclude(
            user=actor.user if actor.is_user else None,
            org=actor.organization if actor.is_org else None
        ).select_related("user__profile", "org__profile").first()

        if not participant:
            return None

        if participant.user:
            return ActorMiniSerializer(participant.user).data

        if participant.org:
            return ActorMiniSerializer(participant.org).data

        return None

    # ----------------------------------------
    # LAST MESSAGE
    # ----------------------------------------
    def get_last_message(self, obj):
        if not obj.last_message:
            return None

        from messaging.serializers.message_serializers import MessageSerializer

        # Pass the context through: without it every nested last_message would
        # build its own ShareViewer and re-query the follow graph once per
        # conversation, and shared previews would render as unavailable.
        return MessageSerializer(obj.last_message, context=self.context).data

    # ----------------------------------------
    # UNREAD COUNT (FIXED)
    # ----------------------------------------
    def get_unread_count(self, obj):
        actor = self.context["request"].actor

        participant = obj.participants.filter(
            user=actor.user if actor.is_user else None,
            org=actor.organization if actor.is_org else None
        ).first()

        qs = obj.messages.filter(is_deleted=False)

        # exclude own messages
        if actor.is_user:
            qs = qs.exclude(sender_user=actor.user)
        else:
            qs = qs.exclude(sender_org=actor.organization)

        if not participant or not participant.last_read_at:
            return qs.count()

        return qs.filter(
            created_at__gt=participant.last_read_at
        ).count()




class ConversationDetailSerializer(serializers.ModelSerializer):

    other_participant = serializers.SerializerMethodField()
    last_message = serializers.SerializerMethodField()
    is_accepted = serializers.SerializerMethodField()
    can_message = serializers.SerializerMethodField()

    unread_count = serializers.SerializerMethodField()
    last_read_at = serializers.SerializerMethodField()
    other_last_read_at = serializers.SerializerMethodField()
    is_last_message_seen = serializers.SerializerMethodField()

    class Meta:
        model = Conversation
        fields = [
            "id",
            "type",
            "status",
            "created_at",

            "last_message",
            "last_message_at",

            "other_participant",

            "is_accepted",
            "can_message",

            "unread_count",
            "last_read_at",
            "other_last_read_at",
            "is_last_message_seen",
        ]

    # OTHER PARTICIPANT
    def get_other_participant(self, obj):
        actor = self.context["request"].actor

        participant = obj.participants.exclude(
            user=actor.user if actor.is_user else None,
            org=actor.organization if actor.is_org else None
        ).select_related("user__profile", "org__profile").first()

        if not participant:
            return None

        if participant.user:
            return ActorMiniSerializer(participant.user).data

        if participant.org:
            return ActorMiniSerializer(participant.org).data

        return None

    # LAST MESSAGE
    def get_last_message(self, obj):
        if not obj.last_message:
            return None

        from messaging.serializers.message_serializers import MessageSerializer

        # Pass the context through: without it every nested last_message would
        # build its own ShareViewer and re-query the follow graph once per
        # conversation, and shared previews would render as unavailable.
        # `other_last_read_at` rides along so the nested message carries a
        # correct `is_read` instead of the no-context default of False.
        return MessageSerializer(
            obj.last_message,
            context={
                **self.context,
                "other_last_read_at": self.get_other_last_read_at(obj),
            },
        ).data

    # REQUEST ACCEPTED?
    def get_is_accepted(self, obj):
        actor = self.context["request"].actor
        participant = obj.participants.filter(
            user=actor.user if actor.is_user else None,
            org=actor.organization if actor.is_org else None
        ).first()

        return participant.has_accepted if participant else False

    # CAN MESSAGE?
    def get_can_message(self, obj):
        actor = self.context["request"].actor
        participant = obj.participants.filter(
            user=actor.user if actor.is_user else None,
            org=actor.organization if actor.is_org else None
        ).first()

        if not participant:
            return False

        # must be accepted
        return participant.has_accepted


    def get_last_read_at(self, obj):
        actor = self.context["request"].actor
        participant = obj.participants.filter(
            user=actor.user if actor.is_user else None,
            org=actor.organization if actor.is_org else None
        ).first()

        return participant.last_read_at if participant else None


    def get_other_last_read_at(self, obj):
        """
        When the OTHER participant last read this thread.

        This is the read-receipt seed: everything the viewer sent at or before
        this instant has been seen. The client keeps it live from the
        ``conversation_read`` websocket event, so it only has to be right at
        the moment the chat opens.
        """
        if not hasattr(self, "_other_last_read_cache"):
            self._other_last_read_cache = {}

        if obj.id in self._other_last_read_cache:
            return self._other_last_read_cache[obj.id]

        actor = self.context["request"].actor
        value = (
            obj.participants
            .exclude(
                user=actor.user if actor.is_user else None,
                org=actor.organization if actor.is_org else None
            )
            .values_list("last_read_at", flat=True)
            .first()
        )

        self._other_last_read_cache[obj.id] = value
        return value


    def get_unread_count(self, obj):
        actor = self.context["request"].actor
        participant = obj.participants.filter(
            user=actor.user if actor.is_user else None,
            org=actor.organization if actor.is_org else None
        ).first()

        qs = obj.messages.filter(is_deleted=False)

        if actor.is_user:
            qs = qs.exclude(sender_user=actor.user)
        else:
            qs = qs.exclude(sender_org=actor.organization)

        if not participant or not participant.last_read_at:
            return qs.count()

        return qs.filter(created_at__gt=participant.last_read_at).count()


    def get_is_last_message_seen(self, obj):
        actor = self.context["request"].actor

        if not obj.last_message:
            return True

        # Check if the actor sent the last message
        if actor.is_user and obj.last_message.sender_user == actor.user:
            return True
        if actor.is_org and obj.last_message.sender_org == actor.organization:
            return True

        participant = obj.participants.filter(
            user=actor.user if actor.is_user else None,
            org=actor.organization if actor.is_org else None
        ).first()

        if not participant or not participant.last_read_at:
            return False

        return obj.last_message.created_at <= participant.last_read_at