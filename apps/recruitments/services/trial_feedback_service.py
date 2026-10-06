# recruitments/services/trial_feedback_service.py
"""
The player's own account of how their trial went.

ITS OWN SERVICE, on purpose. Every other write on an application either moves
``status`` or hangs off something that did. This one must NEVER touch it: the
player's answer is a hint the org reads, not a decision the org made, and the
moment a self-report can move a status the whole separation is gone. Keeping
it out of ``ApplicationService`` makes that structural rather than a comment
somebody has to remember.

WHAT IT DOES NOT DO, and each of these is a decision, not an omission:

  * No status write.
  * No notification, no push, no email. The org was going to open the list
    anyway; a notification per answer would make a busy trial unusable and
    would teach orgs to ignore the ones that matter.
  * No career entry. "I was selected" is a claim, not a verified stint.
  * No ``RecruitmentApplicationStatusHistory`` row. That table answers one
    question — who changed this application's status, and when — and this is
    not a status transition. A row here would pollute the only audit trail
    that exists.

It writes five columns and returns.
"""

from django.db.models import Avg, Count
from django.utils import timezone

from apps.recruitments.models import RecruitmentApplication

# The columns this service owns. Named once so the UPDATE cannot drift into
# touching anything else — `status` in particular.
FEEDBACK_FIELDS = [
    "attended_self_reported",
    "outcome_self_reported",
    "trial_rating",
    "trial_feedback",
    "feedback_at",
]


class TrialFeedbackService:

    @staticmethod
    def submit(application, validated_data):
        """
        Record (or re-record) one player's answer.

        UPDATE IN PLACE, always. A player who answered "still waiting" will
        hear back later and must be able to come back and say "selected" — a
        second row would leave the org reading a stale hint next to a fresh
        one. ``feedback_at`` is re-stamped so the org can see the answer moved.

        The caller has already decided this player may answer at all
        (``feedback_window``) and the serializer has already blanked the
        outcome and the rating for somebody who did not attend.
        """
        application.attended_self_reported = validated_data["attended"]
        application.outcome_self_reported = validated_data.get("outcome", "")
        application.trial_rating = validated_data.get("rating")
        application.trial_feedback = validated_data.get("feedback", "")
        application.feedback_at = timezone.now()

        # An explicit field list, not a bare save(): this is the one write
        # that must be provably incapable of moving `status`.
        application.save(update_fields=FEEDBACK_FIELDS)

        return application

    @staticmethod
    def rating_summary(recruitment):
        """
        ``(average, count)`` over the ratings on one recruitment, as a plain
        aggregate.

        DELIBERATELY NOT DENORMALIZED. A recruitment carries a few hundred
        applications at most, so this is one cheap aggregate on an indexed
        FK — and a counter would be a third thing to keep in sync with a
        column that can be rewritten on every resubmit.
        """
        result = (
            RecruitmentApplication.objects
            .filter(recruitment=recruitment, trial_rating__isnull=False)
            .aggregate(average=Avg("trial_rating"), count=Count("id"))
        )

        average = result["average"]
        return (
            round(average, 1) if average is not None else None,
            result["count"] or 0,
        )
