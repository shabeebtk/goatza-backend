"""
Share a post, a recruitment or a profile into one or more conversations.

Sharing happens from the feed or a profile header and can fan out to several
threads at once, so this is a REST call rather than a websocket send: there is
no single socket to send it on, and the sender may not have any of the target
conversations open.

Every target is independent — one bad conversation id does not sink the rest.
The caller gets {"sent": [...], "failed": [{"id", "reason"}]}.
"""

from accounts.models import User
from messaging.models import Conversation
from messaging.selectors.share_selectors import (
    ShareViewer,
    is_org_profile_shareable,
    is_post_shareable,
    is_recruitment_shareable,
    is_user_profile_shareable,
)
from messaging.services.conversation_service import ConversationService
from messaging.services.exceptions import (
    BlockedParticipantError,
    ContentUnavailableError,
    MessageError,
    RecipientNotFoundError,
    SelfShareError,
)
from messaging.services.message_service import MessageService
from posts.models import Post
from recruitments.models import Recruitment
from organization.models import Organization
from moderation.services.block_guard import require_not_blocked

TARGET_POST = "post"
TARGET_RECRUITMENT = "recruitment"
TARGET_USER = "user"
TARGET_ORGANIZATION = "organization"


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
        existence of followers-only content by watching the status code. The
        profile branches keep that behaviour even though nothing about a profile
        is followers-only: one error code across all four targets means the
        client's failure copy stays uniform.

        Returns (kind, object) where kind is one of the TARGET_* constants.
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

        if target_type == TARGET_USER:
            # Same fan-out reasoning: the preview reads the profile plus the
            # primary sport and position, so join and prefetch them once rather
            # than 10 times.
            profile_user = (
                User.objects
                .select_related("profile")
                .prefetch_related(
                    "sports__sport",
                    "positions__position",
                )
                .filter(id=target_id)
                .first()
            )
            if not is_user_profile_shareable(profile_user, viewer):
                raise ContentUnavailableError(
                    "Profile not found or not available"
                )
            return TARGET_USER, profile_user

        if target_type == TARGET_ORGANIZATION:
            profile_org = (
                Organization.objects
                .select_related("profile")
                .prefetch_related("locations")
                .filter(id=target_id)
                .first()
            )
            if not is_org_profile_shareable(profile_org, viewer):
                raise ContentUnavailableError(
                    "Profile not found or not available"
                )
            return TARGET_ORGANIZATION, profile_org

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
        common = {
            "conversation": conversation,
            "sender_user": sender_user,
            "sender_org": sender_org,
            "note": note,
        }

        if kind == TARGET_POST:
            return MessageService.send_shared_post(post=obj, **common)

        if kind == TARGET_USER:
            return MessageService.send_shared_user_profile(
                profile_user=obj, **common
            )

        if kind == TARGET_ORGANIZATION:
            return MessageService.send_shared_org_profile(
                profile_org=obj, **common
            )

        return MessageService.send_shared_recruitment(
            recruitment=obj, **common
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

        # BLOCK GUARD — as a MessageError, so ONE blocked recipient lands in
        # `failed` and the other nine in the same share still go out. Runs
        # before get_or_create, whose own guard raises the 403 flavour that
        # would fail the whole request.
        require_not_blocked(
            actor, target_user or target_org, error=BlockedParticipantError
        )

        conversation, _ = ConversationService.get_or_create_conversation(
            actor_user=actor.user if actor.is_user else None,
            actor_org=actor.organization if actor.is_org else None,
            target_user=target_user,
            target_org=target_org,
        )

        return conversation
