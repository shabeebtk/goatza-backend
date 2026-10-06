# recruitments/selectors/announcement_selectors.py
"""
Who may read which announcements.

Two rules, and the second is the interesting one:

  * ``all_applicants`` announcements are public to anyone who can already see
    the posting. They are the club's noticeboard — "gates open at 8" is not a
    secret, and a player deciding whether to apply benefits from reading it.
  * a TARGETED announcement (``confirmed`` / ``selected``) is visible only to
    the people it was sent to. "Selected players, report Monday" must not be
    how somebody finds out they were not selected.

"The people it was sent to" is read off the OUTBOX, not recomputed from the
audience. The delivery rows are the snapshot of who was on the list the day it
went out; re-resolving the audience would mean an applicant whose status
changed afterwards either loses a message they already received or gains one
they never did.

The owning org sees everything, deleted rows aside, with a delivery summary —
"Delivered to 138 of 142" is the only way the org can tell a slow drain from a
finished one.
"""

from django.db.models import Q

from apps.recruitments.models import RecruitmentAnnouncement


class AnnouncementSelector:

    @staticmethod
    def is_owner(recruitment, actor):
        return bool(
            actor
            and actor.is_org
            and actor.organization
            and str(actor.organization.id) == str(recruitment.organization_id)
        )

    @staticmethod
    def list_for_actor(recruitment, actor, limit=20, offset=0):
        """
        (page, total_count, is_owner) for one recruitment's announcements.

        Soft-deleted rows are excluded for EVERYONE, the owner included: a
        delete cannot unsend what already left, but it does remove the entry
        from every list, which is the only thing it claims to do.
        """
        queryset = RecruitmentAnnouncement.objects.filter(
            recruitment=recruitment, is_deleted=False
        )

        is_owner = AnnouncementSelector.is_owner(recruitment, actor)

        if not is_owner:
            visible = Q(audience=RecruitmentAnnouncement.Audience.ALL_APPLICANTS)

            # A signed-in player additionally sees anything addressed to them.
            # An org acting as itself on someone else's posting, and an
            # anonymous reader, get the public ones only.
            if actor is not None and actor.is_user and actor.user is not None:
                visible |= Q(deliveries__recipient=actor.user)

            # distinct(): the deliveries join multiplies a row by its channels.
            queryset = queryset.filter(visible).distinct()

        total_count = queryset.count()

        page = queryset.select_related(
            "session__location",
            "created_by_member__user__profile",
        ).order_by("-created_at")[offset: offset + limit]

        return page, total_count, is_owner
