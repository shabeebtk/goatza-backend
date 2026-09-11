"""Throttle for the shortlist write."""

from apps.messaging.throttles import ActorScopedThrottle


class SaveRecruitmentThrottle(ActorScopedThrottle):
    """
    60/min on toggling a save (``recruitment_save`` in
    DEFAULT_THROTTLE_RATES).

    Per ACTOR, not per user: the shortlist itself is per-actor, so a scout
    curating their club's list must not eat the budget for their own. That is
    the same call messaging.throttles.ShareThrottle makes, and the opposite of
    moderation's — a save is private and reversible, so there is no
    brigading leverage to take away.

    Loose because the honest gesture is a tap and the honest correction is a
    second tap: 60/min is far past anyone shortlisting by hand, and far under
    what a script would need to make the row churn cost anything. Its own
    scope so a burst of bookmarks never drains the shared 'user' budget that
    applying to a trial draws on.
    """

    scope = "recruitment_save"
