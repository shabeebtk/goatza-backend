"""
Share a post or recruitment into one or more conversations.

Sharing happens from the feed and can fan out to several threads at once, so
this is a REST call rather than a websocket send: there is no single socket to
send it on, and the sender may not have any of the target conversations open.

Every target is independent — one bad conversation id does not sink the rest.
The caller gets {"sent": [...], "failed": [{"id", "reason"}]}.
"""

from messaging.models import Conversation
from messaging.selectors.share_selectors import (
    ShareViewer,
    is_post_shareable,
    is_recruitment_shareable,
)
from messaging.services.conversation_service import ConversationService
from messaging.services.exceptions import (
    ContentUnavailableError,
    MessageError,
    RecipientNotFoundError,
    SelfShareError,
)
from messaging.services.message_service import MessageService
from posts.models import Post
from recruitments.models import Recruitment
from accounts.models import User
from organization.models import Organization

TARGET_POST = "post"
TARGET_RECRUITMENT = "recruitment"


class ShareService:

    # ----------------------------------------
    # TARGET RESOLUTION
    # ----------------------------------------
    @staticmethod
    def resolve_target(target_type, target_id, actor):
        """
        Fetch the object being shared and confirm the sender may see it.

        Raises ContentUnavailableError for BOTH "does not exist" and "exists but
        you cannot see it" — distinguishing them would let anyone probe for the
        existence of followers-only content by watching the status code.

        Returns (kind, object) where kind is TARGET_POST / TARGET_RECRUITMENT.
        """
        viewer = ShareViewer.from_actor(actor)

        if target_type == TARGET_POST:
            # The same instance is reused for every target, and each send
            # re-serializes it for the realtime payload — prefetching media here
            # keeps a 10-conversation fan-out from re-querying it 10 times.
            post = (
                Post.objects
                .select_related("author_user__profile", "author_org__profile")
                .prefetch_related("media")
                .filter(id=target_id)
                .first()
            )
            if not is_post_shareable(post, viewer):
                raise ContentUnavailableError("Post not found or not available")
            return TARGET_POST, post

        recruitment = (
            Recruitment.objects
            .select_related("organization__profile", "sport")
            .prefetch_related("media")
            .filter(id=target_id)
            .first()
        )
        if not is_recruitment_shareable(recruitment, viewer):
            raise ContentUnavailableError(
                "Recruitment not found or not available"
            )
        return TARGET_RECRUITMENT, recruitment

    # ----------------------------------------
    # MAIN ENTRY
    # ----------------------------------------
    @staticmethod
    def share(
        actor,
        target_type,
        target_id,
        conversation_ids=None,
        recipients=None,
        note="",
    ):
        kind, obj = ShareService.resolve_target(target_type, target_id, actor)

        sender_user = actor.user if actor.is_user else None
        sender_org = actor.organization if actor.is_org else None

        sent = []
        failed = []
        # A recipient may also appear in conversation_ids (the client resolved
        # the thread and passed both). Send once.
        seen_conversations = set()

        def deliver(conversation, report_id):
            if str(conversation.id) in seen_conversations:
                return
            seen_conversations.add(str(conversation.id))

            try:
                ShareService._send(
                    kind, obj, conversation, sender_user, sender_org, note
                )
                sent.append(str(conversation.id))
            except MessageError as exc:
                failed.append({"id": report_id, "reason": exc.reason})

        # ── EXISTING CONVERSATIONS ───────────────────────────────
        for conversation_id in (conversation_ids or []):
            conversation = Conversation.objects.filter(
                id=conversation_id
            ).first()

            if not conversation:
                failed.append({
                    "id": str(conversation_id),
                    "reason": "conversation_not_found",
                })
                continue

            # Participation is re-checked inside MessageService, which raises
            # NotParticipantError — that lands in `failed`, never a 500.
            deliver(conversation, str(conversation_id))

        # ── RECIPIENTS WITH NO CONVERSATION YET ──────────────────
        for recipient in (recipients or []):
            actor_id = str(recipient.get("actor_id"))

            try:
                conversation = ShareService._conversation_for_recipient(
                    actor, recipient
                )
            except MessageError as exc:
                failed.append({"id": actor_id, "reason": exc.reason})
                continue

            deliver(conversation, actor_id)

        return {"sent": sent, "failed": failed}

    # ----------------------------------------
    @staticmethod
    def _send(kind, obj, conversation, sender_user, sender_org, note):
        if kind == TARGET_POST:
            return MessageService.send_shared_post(
                conversation=conversation,
                sender_user=sender_user,
                sender_org=sender_org,
                post=obj,
                note=note,
            )

        return MessageService.send_shared_recruitment(
            conversation=conversation,
            sender_user=sender_user,
            sender_org=sender_org,
            recruitment=obj,
            note=note,
        )

    # ----------------------------------------
    @staticmethod
    def _conversation_for_recipient(actor, recipient):
        """
        Get-or-create the direct conversation with a recipient.

        Goes through ConversationService, so the direct_pair_key de-dup and the
        request/accept rules apply unchanged: share with a stranger and the
        thread is created as REQUESTED with the recipient's has_accepted=False,
        i.e. it lands in their Requests tab exactly like a first text would.
        """
        actor_type = recipient.get("actor_type")
        actor_id = recipient.get("actor_id")

        target_user = None
        target_org = None

        if actor_type == "user":
            target_user = User.objects.filter(id=actor_id).first()
            if not target_user:
                raise RecipientNotFoundError("Recipient not found")
            if actor.is_user and actor.user.id == target_user.id:
                raise SelfShareError("Cannot share with yourself")
        else:
            target_org = Organization.objects.filter(
                id=actor_id, is_active=True
            ).first()
            if not target_org:
                raise RecipientNotFoundError("Recipient not found")
            if actor.is_org and actor.organization.id == target_org.id:
                raise SelfShareError("Cannot share with your own organization")

        conversation, _ = ConversationService.get_or_create_conversation(
            actor_user=actor.user if actor.is_user else None,
            actor_org=actor.organization if actor.is_org else None,
            target_user=target_user,
            target_org=target_org,
        )

        return conversation
