from asgiref.sync import async_to_sync
from django.template.defaultfilters import default
from django.db import transaction, IntegrityError
from django.db.models import Q, Count
from messaging.models import Conversation, ConversationParticipant
from connections.services.follow_services import FollowService
from connections.models import Follow
from accounts.models import User
from organization.models import Organization
from moderation.services.block_guard import require_not_blocked
from moderation.selectors.blocked_filters import blocked_id_sets
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

        # BLOCK GUARD — before the lookup, so a blocked pair can neither open a
        # new thread nor re-surface an old one as a message request. Raises
        # BlockedError (403); ShareService guards its own recipients first with
        # the MessageError flavour so a fan-out still reports per target.
        require_not_blocked(
            actor_user or actor_org, target_user or target_org
        )

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
    def is_participants(conversation, actor):
        return ConversationParticipant.objects.filter(
            conversation=conversation,
            user=actor.user if actor.is_user else None,
            org=actor.organization if actor.is_org else None
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

        # Stamped once and reused for the broadcast — re-reading
        # timezone.now() would hand clients a timestamp slightly ahead of the
        # row, and messages sent in that sliver would look read before they
        # were.
        read_at = timezone.now()

        updated = query.update(
            last_read_at=read_at
        )

        if not updated:
            raise Exception("Participant not found")

        ConversationService._trigger_realtime_read(
            conversation,
            reader_id=str(user.id if user else org.id),
            read_at=read_at
        )

        return True

    @staticmethod
    def _trigger_realtime_read(conversation, reader_id, read_at):
        """
        Tell the other side's open chat window that everything up to
        ``read_at`` has been seen, so their sent bubbles turn blue without a
        refetch — the sender half of the read receipt.

        Best-effort by design: a dead channel layer must never fail a read the
        user has already been shown as done. The ticks then catch up from
        ``is_read`` the next time the thread loads.
        """
        from channels.layers import get_channel_layer

        try:
            channel_layer = get_channel_layer()

            if channel_layer is None:
                return

            async_to_sync(channel_layer.group_send)(
                f"chat_{conversation.id}",
                {
                    "type": "conversation_read",
                    "reader_id": reader_id,
                    # isoformat, not the raw datetime: this dict is msgpack'd
                    # onto the channel layer, which only carries primitives.
                    "last_read_at": read_at.isoformat(),
                }
            )
        except Exception:
            pass

    # ----------------------------------------
    # MESSAGE TARGET SEARCH
    # ----------------------------------------
    @staticmethod
    def search_message_targets(actor, query, limit=20):
        """
        Prioritised people / organization search for starting or resuming a chat
        from the messages screen.

        Result ordering (deduped by identity, the actor itself excluded):
          1. Actors the current actor already has a direct conversation with.
          2. Actors the current actor follows.
          3. Everyone else (global user + organization search).

        Each item carries ``conversation_id`` when a direct conversation already
        exists, so the client can open it directly instead of round-tripping
        through get-or-create. Items without one are opened via the normal
        get-or-create flow (same as the profile "Message" button).
        """
        query = (query or "").strip()
        if not query:
            return []

        limit = max(1, min(int(limit or 20), 30))

        actor_user = actor.user if actor.is_user else None
        actor_org = actor.organization if actor.is_org else None

        # BLOCK EXCLUSION — this search feeds three prioritised sources
        # (existing threads, follows, global) into one list, so it filters in
        # the two add_* closures rather than on any single queryset.
        blocked_user_ids, blocked_org_ids = blocked_id_sets(actor)

        results = []
        seen_users = set()
        seen_orgs = set()

        def remaining():
            return limit - len(results)

        def add_user(user, conversation_id=None, source=""):
            if user is None or user.id in seen_users:
                return
            if actor_user and user.id == actor_user.id:
                return
            if user.id in blocked_user_ids:
                return
            seen_users.add(user.id)
            results.append(
                ConversationService._target_item_user(user, conversation_id, source)
            )

        def add_org(org, conversation_id=None, source=""):
            if org is None or org.id in seen_orgs:
                return
            if actor_org and org.id == actor_org.id:
                return
            if org.id in blocked_org_ids:
                return
            seen_orgs.add(org.id)
            results.append(
                ConversationService._target_item_org(org, conversation_id, source)
            )

        match_user = (
            Q(user__username__icontains=query)
            | Q(user__profile__name__icontains=query)
        )
        match_org = (
            Q(org__username__icontains=query)
            | Q(org__name__icontains=query)
        )

        # ----------------------------------------
        # 1. EXISTING CONVERSATIONS
        # ----------------------------------------
        # Conversation ids the actor participates in …
        my_conversation_ids = list(
            ConversationParticipant.objects.filter(
                user=actor_user,
                org=actor_org,
            ).values_list("conversation_id", flat=True)
        )

        if my_conversation_ids:
            # … then the OTHER participant of each, matching the query. Excluding
            # the actor's own participant row means the query is matched only
            # against the person we'd actually be messaging.
            other_participants = (
                ConversationParticipant.objects
                .filter(conversation_id__in=my_conversation_ids)
                .exclude(user=actor_user, org=actor_org)
                .filter(match_user | match_org)
                .select_related("user__profile", "org__profile")
                .order_by("-conversation__last_message_at", "-conversation__created_at")
            )

            for part in other_participants:
                if remaining() <= 0:
                    break
                if part.user_id:
                    add_user(part.user, conversation_id=part.conversation_id, source="conversation")
                elif part.org_id:
                    add_org(part.org, conversation_id=part.conversation_id, source="conversation")

        # ----------------------------------------
        # 2. FOLLOWINGS
        # ----------------------------------------
        if remaining() > 0:
            if actor_user:
                follows = Follow.objects.filter(follower_user=actor_user)
            else:
                follows = Follow.objects.filter(follower_org=actor_org)

            follows = (
                follows.filter(
                    Q(following_user__username__icontains=query)
                    | Q(following_user__profile__name__icontains=query)
                    | Q(following_org__username__icontains=query)
                    | Q(following_org__name__icontains=query)
                )
                .select_related("following_user__profile", "following_org__profile")
                .order_by("-created_at")
            )

            for follow in follows:
                if remaining() <= 0:
                    break
                if follow.following_user_id:
                    add_user(follow.following_user, source="following")
                elif follow.following_org_id:
                    add_org(follow.following_org, source="following")

        # ----------------------------------------
        # 3. EVERYONE ELSE (global search)
        # ----------------------------------------
        if remaining() > 0:
            users = (
                User.objects
                .filter(Q(username__icontains=query) | Q(profile__name__icontains=query))
                .select_related("profile")
            )
            if actor_user:
                users = users.exclude(id=actor_user.id)
            if seen_users:
                users = users.exclude(id__in=seen_users)

            for user in users.order_by("username")[: remaining()]:
                add_user(user, source="all")

        if remaining() > 0:
            orgs = (
                Organization.objects
                .filter(is_active=True)
                .filter(Q(username__icontains=query) | Q(name__icontains=query))
                .select_related("profile")
            )
            if actor_org:
                orgs = orgs.exclude(id=actor_org.id)
            if seen_orgs:
                orgs = orgs.exclude(id__in=seen_orgs)

            for org in orgs.order_by("name")[: remaining()]:
                add_org(org, source="all")

        return results

    @staticmethod
    def _target_item_user(user, conversation_id=None, source=""):
        profile = getattr(user, "profile", None)
        return {
            "id": str(user.id),
            "username": user.username,
            "name": (getattr(profile, "name", "") or "") if profile else "",
            "avatar": (getattr(profile, "profile_photo", "") or "") if profile else "",
            "headline": (getattr(profile, "headline", "") or "") if profile else "",
            "type": "user",
            "conversation_id": str(conversation_id) if conversation_id else None,
            "source": source,
        }

    @staticmethod
    def _target_item_org(org, conversation_id=None, source=""):
        profile = getattr(org, "profile", None)
        return {
            "id": str(org.id),
            "username": org.username,
            "name": org.name or "",
            "avatar": (getattr(profile, "logo", "") or "") if profile else "",
            "headline": (getattr(profile, "headline", "") or "") if profile else "",
            "type": "organization",
            "conversation_id": str(conversation_id) if conversation_id else None,
            "source": source,
        }

