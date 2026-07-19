"""Read queries for messages."""

from django.db.models import Prefetch

from messaging.models import Message
from posts.models import PostMedia
from recruitments.models import RecruitmentMedia


class MessageSelector:

    @staticmethod
    def list_messages(conversation_id):
        """
        Messages for a conversation, newest first, with everything
        MessageSerializer touches joined in.

        The prefetches matter: the shared previews read author profiles, the
        sport, and the first media row of the shared object. Without them a
        20-message page fans out into ~100 queries.
        """
        return (
            Message.objects
            .filter(conversation_id=conversation_id, is_deleted=False)
            .select_related(
                "sender_user__profile",
                "sender_org__profile",
                "shared_post__author_user__profile",
                "shared_post__author_org__profile",
                "shared_recruitment__organization__profile",
                "shared_recruitment__sport",
            )
            .prefetch_related(
                # Ordering here (not in the serializer) keeps the prefetch
                # cache usable — re-ordering in Python would re-query.
                Prefetch(
                    "shared_post__media",
                    queryset=PostMedia.objects.order_by("order"),
                ),
                Prefetch(
                    "shared_recruitment__media",
                    queryset=RecruitmentMedia.objects.order_by("order"),
                ),
            )
            .order_by("-created_at")
        )
