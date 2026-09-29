# recruitments/trial_window.py
"""
THE definition of "this trial is over" — the WHOLE trial window, not the
apply gate.

A trial runs on one or more ``TrialSession`` dates. The last of them is
cached on the recruitment as ``trial_end_date`` (23:59:59 of that day, written
by ``RecruitmentService._sync_trial_window``), so the rule is about the
CALENDAR DAY, not the instant: a trial is over once its last day has ended in
``settings.RECRUITMENT_TIMEZONE``. A 09:00 session is still "today" at 20:00;
the trial is over at 00:00 the morning after its final date. A three-weekend
trial is NOT over after weekend one.

This module deliberately does not answer "can somebody still apply". That is
``Recruitment.applications_close_at``, and it usually says an EARLIER instant
— on an "attend every date" trial applications close on the FIRST session,
because a player cannot join a two-day trial on day two.

Three spellings of one rule, all built on ``start_of_today``:

  * ``trial_not_over_q``  — the queryset half, for the player-facing lists
  * ``is_trial_over``     — the Python half, behind ``Recruitment.is_trial_over``
  * ``start_of_today``    — the boundary both compare against

Kept in its own module so ``models.py`` and the selectors can share it without
either importing the other. ``now`` is injectable everywhere for tests.
"""

from zoneinfo import ZoneInfo

from django.conf import settings
from django.db.models import Q
from django.utils import timezone


def start_of_today(now=None):
    """
    Midnight at the start of the current day in RECRUITMENT_TIMEZONE, as an
    aware datetime. Comparable directly against the UTC datetimes the ORM
    hands back and accepts.
    """
    now = now or timezone.now()
    local = now.astimezone(ZoneInfo(settings.RECRUITMENT_TIMEZONE))
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


def trial_not_over_q(now=None):
    """
    Rows whose trial window has not closed: no trial_end_date at all, or one
    on or after the start of today. A trial whose last day is today therefore
    stays in.

    A null bound never excludes — which is why every row must be BACKFILLED
    before this ships. A pre-backfill recruitment has a null trial_end_date
    and would pass this filter forever, putting every ended trial back in the
    player-facing lists. Run ``manage.py backfill_trial_sessions`` first.
    """
    return (
        Q(trial_end_date__isnull=True)
        | Q(trial_end_date__gte=start_of_today(now))
    )


def is_trial_over(trial_end_date, now=None):
    """The same rule for one row. No trial_end_date → never over."""
    if trial_end_date is None:
        return False
    return trial_end_date < start_of_today(now)
