
from rest_framework import serializers
from messaging.models import Conversation, ConversationParticipant
from accounts.serializers.user_serializers import UserMiniSerializer
from shared.serializers.ActorSerializer import ActorMiniSerializer

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
        return MessageSerializer(obj.last_message).data

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

    other_user = serializers.SerializerMethodField()
    last_message = serializers.SerializerMethodField()
    is_accepted = serializers.SerializerMethodField()
    can_message = serializers.SerializerMethodField()

    unread_count = serializers.SerializerMethodField()
    last_read_at = serializers.SerializerMethodField()
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

            "other_user",

            "is_accepted",
            "can_message",

            "unread_count",
            "last_read_at",
            "is_last_message_seen",
        ]

    # OTHER USER
    def get_other_user(self, obj):
        request_user = self.context["request"].user

        participant = (
            obj.participants
            .exclude(user=request_user)
            .select_related("user__profile")
            .first()
        )

        if participant and participant.user:
            return UserMiniSerializer(participant.user).data

        return None

    # LAST MESSAGE
    def get_last_message(self, obj):
        if not obj.last_message:
            return None

        from messaging.serializers.message_serializers import MessageSerializer
        return MessageSerializer(obj.last_message).data

    # REQUEST ACCEPTED?
    def get_is_accepted(self, obj):
        request_user = self.context["request"].user

        participant = obj.participants.filter(user=request_user).first()

        return participant.has_accepted if participant else False

    # CAN MESSAGE?
    def get_can_message(self, obj):
        request_user = self.context["request"].user

        participant = obj.participants.filter(user=request_user).first()

        if not participant:
            return False

        # must be accepted
        return participant.has_accepted


    def get_last_read_at(self, obj):
        request_user = self.context["request"].user

        participant = obj.participants.filter(user=request_user).first()

        return participant.last_read_at if participant else None


    def get_unread_count(self, obj):
        request_user = self.context["request"].user

        participant = obj.participants.filter(user=request_user).first()

        if not participant or not participant.last_read_at:
            return obj.messages.filter(is_deleted=False).exclude(sender_user=request_user).count()

        return obj.messages.filter(
            created_at__gt=participant.last_read_at,
            is_deleted=False
        ).exclude(sender_user=request_user).count()


    def get_is_last_message_seen(self, obj):
        request_user = self.context["request"].user

        if not obj.last_message:
            return True

        if obj.last_message.sender_user == request_user:
            return True

        participant = obj.participants.filter(user=request_user).first()

        if not participant or not participant.last_read_at:
            return False

        return obj.last_message.created_at <= participant.last_read_at