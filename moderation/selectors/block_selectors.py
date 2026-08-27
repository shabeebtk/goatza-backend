"""
Read queries for blocking.

Only ONE direction is ever listed: the blocks an actor MADE. The rows aimed at
an actor are deliberately unreadable to them — the settings screen is "accounts
you blocked", never "accounts that blocked you", and surfacing the second is
the disclosure the symmetric design in BlockService exists to avoid.
"""

from moderation.models import Block


class BlockSelector:

    @staticmethod
    def get_blocked_list(actor, limit=20, offset=0):
        """
        The acting actor's blocked list, newest first.

        Returns ``(page_queryset, total_count)`` — the COUNT is taken before
        slicing so "showing 20 of 63" is honest, same shape as
        ApplicationSelector.list_applications.

        select_related covers all four FK columns plus the two profiles the
        item serializer reads (name/avatar), so rendering a page is one query
        rather than one per row.
        """
        if actor is None:
            return Block.objects.none(), 0

        if actor.is_user:
            queryset = Block.objects.filter(blocker_user=actor.user)
        else:
            queryset = Block.objects.filter(blocker_org=actor.organization)

        queryset = queryset.select_related(
            "blocker_user__profile",
            "blocker_org__profile",
            "blocked_user__profile",
            "blocked_org__profile",
        )

        total_count = queryset.count()

        page = queryset.order_by("-created_at")[offset: offset + limit]

        return page, total_count
