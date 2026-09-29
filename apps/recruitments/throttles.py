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


class TrialFeedbackThrottle(ActorScopedThrottle):
    """
    20/min on the player's own account of a trial (``recruitment_feedback``
    in DEFAULT_THROTTLE_RATES).

    Per ACTOR like its neighbour, though in practice this is always a player
    acting as themselves — an org has no reason to call it.

    MODEST rather than loose. Nobody spams their own feedback: the honest
    shape is one answer per trial, plus a correction weeks later when
    "waiting" turns into "selected". But it is a write open to any logged-in
    player, so it gets a ceiling. Its own scope so a burst never drains the
    shared 'user' budget that applying to the next trial draws on.
    """

    scope = "recruitment_feedback"
