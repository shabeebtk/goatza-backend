# recruitments/selectors/saved_recruitment_selectors.py
"""
Reads for the shortlist: "did this actor save it" and "what has this actor
saved".

Saving a recruitment is per-ACTOR, not per-person — acting as an organization
saves to that org's list, and the same recruitment can be saved independently
by a user and by an org they run. Same rule posts.services.saved_post_service
states for saved posts, and the same reason: the dual-actor identity is the
product, not an implementation detail.

``annotate_is_saved`` must be applied to EVERY queryset that reaches a
recruitment card or detail serializer. A missing annotation renders an empty
bookmark on a recruitment the viewer has actually shortlisted, which reads as
data loss — so the serializer defaults to False rather than raising, and the
call sites are the thing to keep honest.
"""

from django.db.models import BooleanField, Exists, OuterRef, Value

from apps.recruitments.models import SavedRecruitment


class SavedRecruitmentSelector:

    @staticmethod
    def actor_filter(actor):
        """The SavedRecruitment column this actor owns, or None if there isn't one."""
        if actor and actor.is_user:
            return {"user": actor.user}
        if actor and actor.is_org:
            return {"org": actor.organization}
        return None

    @staticmethod
    def annotate_is_saved(queryset, actor):
        """
        Add ``is_saved`` for the CURRENT actor to a recruitment queryset.

        Exists() rather than a join, for the same reason the posts annotation
        uses it: a join widens the row set and would force a distinct() that
        fights the birth-year filter's own distinct() and the offset paginator.
        """
        saved_by_actor = SavedRecruitmentSelector.actor_filter(actor)

        if saved_by_actor is None:
            # No resolvable actor (anonymous / public org profile) — still emit
            # the key so the client never has to distinguish "not saved" from
            # "field absent".
            return queryset.annotate(
                is_saved=Value(False, output_field=BooleanField())
            )

        return queryset.annotate(
            is_saved=Exists(
                SavedRecruitment.objects.filter(
                    recruitment=OuterRef("pk"), **saved_by_actor
                )
            )
        )

    @staticmethod
    def saved_ids(actor, recruitment_ids):
        """
        Which of ``recruitment_ids`` this actor has saved, as a set of UUIDs.

        The annotation's answer for a page that was already serialized — the
        discover payload is cached for ten minutes and a bookmark is not, so
        that endpoint re-reads the flags on a cache hit. One query for the
        page, same as the Exists subquery costs on a fresh build.
        """
        saved_by_actor = SavedRecruitmentSelector.actor_filter(actor)
        if saved_by_actor is None or not recruitment_ids:
            return set()

        return set(
            SavedRecruitment.objects
            .filter(recruitment_id__in=recruitment_ids, **saved_by_actor)
            .values_list("recruitment_id", flat=True)
        )

    @staticmethod
    def saved_rows(actor):
        """
        The actor's save rows, most recently SAVED first.

        Ordered by the save, not by the recruitment: re-saving an old posting
        should put it back at the top, which is when it became something they
        wanted again.

        FILTERING (§ shortlist rules):

          - ``is_deleted`` recruitments drop out. The org withdrew the posting;
            there is nothing left to shortlist.
          - Recruitments the caller can no longer SEE drop out too — the
            visibility clause is ``RecruitmentSelector.visible_to_actor_queryset``,
            so a posting flipped to private, or a followers-only one the viewer
            has since unfollowed, disappears from the list exactly as it
            disappears everywhere else. One source of truth; nothing about
            visibility is re-decided here.
          - Non-ACTIVE recruitments deliberately STAY. Closed and cancelled
            postings are the whole point of a shortlist — it is where someone
            notices a deadline passed — and the status is already on the card
            payload, so the client shows the badge.
        """
        saved_by_actor = SavedRecruitmentSelector.actor_filter(actor)
        if saved_by_actor is None:
            return SavedRecruitment.objects.none()

        # Imported here, not at module scope: recruitment_selectors imports
        # THIS module for annotate_is_saved, and the two would deadlock at
        # import time. The dependency is real either way — visibility has one
        # home and this is it.
        from apps.recruitments.selectors.recruitment_selectors import (
            RecruitmentSelector,
        )

        visible = RecruitmentSelector.visible_to_actor_queryset(actor)

        return (
            SavedRecruitment.objects
            .filter(recruitment_id__in=visible.values("id"), **saved_by_actor)
            .order_by("-created_at", "-id")
        )
