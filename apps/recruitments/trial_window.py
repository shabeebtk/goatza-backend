# recruitments/trial_window.py
"""
THE definition of "this trial is over" — the WHOLE trial window, not the
apply gate.

THE RULE: ``trial_end_date`` has passed. That is the entire comparison, and
there is no timezone anywhere in it.

WHY IT IS THAT SIMPLE. A trial runs on one or more ``TrialSession`` dates,
and the last of them is cached on the recruitment as ``trial_end_date`` —
23:59:59 of that day **in the recruitment's own timezone**, resolved once by
``RecruitmentService._sync_trial_window`` and stored as the UTC instant. The
calendar-day semantics therefore live at WRITE time, where a single row has a
single answer. By the time anything reads the column, the day boundary for
that trial's country is already baked into it: a London trial's end instant
is 23:59:59 Europe/London, an Indian one's is 23:59:59 Asia/Kolkata, and
comparing either against ``now()`` is correct for both.

This matters because one half of the rule is a QUERYSET filter. With a
per-row timezone there is no single "start of today" to compare against, and
making the read timezone-aware would mean converting zones in SQL. Doing the
maths on the way IN removes the problem rather than solving it.

A 09:00 session is still "today" at 20:00 at the venue; the trial is over at
00:00 the morning after its final date, there. A three-weekend trial is NOT
over after weekend one.

This module deliberately does not answer "can somebody still apply". That is
``Recruitment.applications_close_at``, and it usually says an EARLIER instant
— on an "attend every date" trial applications close on the FIRST session,
because a player cannot join a two-day trial on day two.

Two spellings of one rule:

  * ``trial_not_over_q``  — the queryset half, for the player-facing lists
  * ``is_trial_over``     — the Python half, behind ``Recruitment.is_trial_over``

Kept in its own module so ``models.py`` and the selectors can share it without
either importing the other. ``now`` is injectable everywhere for tests.
"""

from django.db.models import Q
from django.utils import timezone


def trial_not_over_q(now=None):
    """
    Rows whose trial window has not closed: no trial_end_date at all, or one
    that has not passed yet. A trial whose last day is today at the venue
    therefore stays in — its end instant is that evening, local.

    A null bound never excludes — which is why every row must be BACKFILLED
    before this ships. A pre-backfill recruitment has a null trial_end_date
    and would pass this filter forever, putting every ended trial back in the
    player-facing lists. Run ``manage.py backfill_trial_sessions`` first.
    """
    now = now or timezone.now()
    return (
        Q(trial_end_date__isnull=True)
        | Q(trial_end_date__gte=now)
    )


def is_trial_over(trial_end_date, now=None):
    """The same rule for one row. No trial_end_date → never over."""
    if trial_end_date is None:
        return False
    return trial_end_date < (now or timezone.now())
