# recruitments/legacy_status.py
"""
The two retired application statuses, and the one rule that translates them.

``invited`` and ``rejected`` stopped being settable when the v3 status split
shipped: ``invited`` became ``trial_confirmed``, and ``rejected`` split into
the honest pair ``not_shortlisted`` (never called to the trial) and
``not_selected`` (came, and did not make it).

WHY THIS MODULE EXISTS. Goatza is an installed PWA, so old JavaScript lives on
phones for days after a deploy. A stale client still POSTs ``rejected``, and
without this the status endpoint answers "Invalid target status." — a 400, and
an org's decision silently lost. So the server ACCEPTS the old words and
translates them.

THE RULE IS SHARED ON PURPOSE. ``migrate_recruitment_v3`` applies exactly this
rule to historical rows, and the live endpoint applies it to what a stale
client sends today. They are the same question asked of different inputs, and
if they ever disagreed the same application would be classified one way by the
backfill and another way by a phone that had not reloaded. ``is_before_trial``
is the one implementation; both call it.

THIS MODULE OUTLIVES THE CHOICES. A later deploy removes ``invited`` and
``rejected`` from ``Status.choices``, and this mapping stays exactly as it is —
its whole job is turning a word that no longer exists into one that does.
"""

from zoneinfo import ZoneInfo

from django.conf import settings

# The retired values, as STRING LITERALS rather than enum members. They are
# deliberately not in Status.choices any more, so there is nothing to
# reference — and that is the point: this module is what still understands
# them.
LEGACY_INVITED = "invited"
LEGACY_REJECTED = "rejected"

LEGACY_STATUSES = (LEGACY_INVITED, LEGACY_REJECTED)


def local_date(value):
    """A stored datetime as its calendar date in RECRUITMENT_TIMEZONE."""
    return value.astimezone(ZoneInfo(settings.RECRUITMENT_TIMEZONE)).date()


def is_before_trial(*, recruitment_type, trial_starts_at, decided_on):
    """
    Was this decision made BEFORE the trial began?

    That is the whole question behind the ``rejected`` split: a club that said
    no before the trial never called the player in (``not_shortlisted``); one
    that said no afterwards watched them play (``not_selected``). Getting it
    backwards tells a player they were rejected on the day when nobody ever
    saw them, or the reverse.

    BY CALENDAR DAY in RECRUITMENT_TIMEZONE, matching every other date rule
    here: a decision made ON the trial day is a real result, not a pre-trial
    screening.

    ``recruitment_type`` must be the EFFECTIVE type (what the recruitment is
    after the v3 type fold), because only an open trial has a trial day at
    all. No trial date → never "before": there is no day to be before.
    """
    from apps.recruitments.models import Recruitment

    if recruitment_type != Recruitment.Type.OPEN_TRIAL:
        return False

    if trial_starts_at is None:
        return False

    return decided_on < local_date(trial_starts_at)


def map_legacy_status(status, *, recruitment_type, trial_starts_at, decided_on):
    """
    A legacy status value as the one that replaced it, or None when the value
    was never legacy (every current status passes straight through untouched).

        invited  -> trial_confirmed
        rejected -> not_shortlisted, decided before the trial
                 -> not_selected, decided on or after it, or on any
                    non-open-trial posting
    """
    from apps.recruitments.models import RecruitmentApplication

    if status == LEGACY_INVITED:
        return RecruitmentApplication.Status.TRIAL_CONFIRMED

    if status == LEGACY_REJECTED:
        before = is_before_trial(
            recruitment_type=recruitment_type,
            trial_starts_at=trial_starts_at,
            decided_on=decided_on,
        )
        return (
            RecruitmentApplication.Status.NOT_SHORTLISTED
            if before
            else RecruitmentApplication.Status.NOT_SELECTED
        )

    return None
