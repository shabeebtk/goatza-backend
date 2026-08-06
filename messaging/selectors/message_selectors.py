"""Read queries for messages."""

from django.db.models import Prefetch

from messaging.models import Message
from posts.models import PostMedia
from recruitments.models import RecruitmentMedia
from sports.models import UserSport, UserSportPosition

# Everything a shared-message preview reads off the shared object. Declared
# once here because the conversation LIST and DETAIL endpoints join the same
# things onto `last_message` — see messaging/views/conversation_views.py, which
# spells them out with a `last_message__` prefix.
SHARED_SELECT_RELATED = (
    "shared_post__author_user__profile",
    "shared_post__author_org__profile",
    "shared_recruitment__organization__profile",
    "shared_recruitment__sport",
    "shared_profile_user__profile",
    "shared_profile_org__profile",
)


class MessageSelector:

    @staticmethod
    def list_messages(conversation_id):
        """
        Messages for a conversation, newest first, with everything
        MessageSerializer touches joined in.

        The prefetches matter: the shared previews read author profiles, the
        sport, the first media row of the shared object, and — for a shared
        profile — the target's primary sport and position. Without them a
        20-message page fans out into ~100 queries. See the assertNumQueries
        tests in messaging/tests.py, which assert that 16 shared messages cost
        exactly what 2 do.
        """
        return (
            Message.objects
            .filter(conversation_id=conversation_id, is_deleted=False)
            .select_related(
                "sender_user__profile",
                "sender_org__profile",
                *SHARED_SELECT_RELATED,
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
                # The profile card prints one sport and one position, but the
                # rows have to be fetched to find the primary ones. Narrowed to
                # is_primary here so the prefetch carries at most one row per
                # shared user instead of their whole sport history — the
                # serializer's `next(... if s.is_primary)` then reads a list of
                # one.
                Prefetch(
                    "shared_profile_user__sports",
                    queryset=UserSport.objects
                    .filter(is_primary=True)
                    .select_related("sport"),
                ),
                Prefetch(
                    "shared_profile_user__positions",
                    queryset=UserSportPosition.objects
                    .filter(is_primary=True)
                    .select_related("position"),
                ),
                "shared_profile_org__locations",
            )
            .order_by("-created_at")
        )
