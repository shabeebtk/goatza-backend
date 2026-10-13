# recruitments/removal.py
"""
What deleting a recruitment means for the people who applied to it.

ONE HOME, like ``trial_window`` and ``feedback_window``: the delete check the
client reads, the delete itself, and the fan-out task all ask "who is still
waiting on this trial?", and three answers would drift into a dialog that
promises a note to 12 players while 14 get it.

STILL IN PLAY means the player is waiting on the club, or was told to turn up:

    applied · reviewing · shortlisted · trial_confirmed · selected
    (+ the legacy ``invited``, which became trial_confirmed)

``withdrawn`` left on their own, and ``not_shortlisted`` / ``not_selected``
(+ legacy ``rejected``) already have their answer. Those are never messaged
about a removal — telling somebody who was turned down that the trial is gone
is noise at best.
"""

from rest_framework.exceptions import ValidationError

from apps.recruitments.legacy_status import LEGACY_INVITED
from apps.recruitments.models import RecruitmentApplication

Status = RecruitmentApplication.Status

IN_PLAY_STATUSES = (
    Status.APPLIED,
    Status.REVIEWING,
    Status.SHORTLISTED,
    Status.TRIAL_CONFIRMED,
    Status.SELECTED,
    LEGACY_INVITED,
)

# What every player-side action answers once the posting is gone.
REMOVED_MESSAGE = "This recruitment was removed by the club."


def refuse_if_removed(recruitment):
    """
    400 for a player acting on an application whose recruitment was deleted.

    The application itself stays on their Applied tab (it is their history),
    so its id still resolves — this is what stops a withdraw, a feedback
    answer or a pass being served for a trial that no longer exists.
    """
    if recruitment.is_deleted:
        raise ValidationError(REMOVED_MESSAGE)
