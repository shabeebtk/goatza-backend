# recruitments/trial_window.py
"""
THE definition of "this trial is over".

A recruitment's ``event_date`` is the trial day. The frontend stores a
date-only trial as 23:59 local time and a timed trial at its real time, so the
rule is about the CALENDAR DAY, not the instant: a trial is over once that day
has ended in ``settings.RECRUITMENT_TIMEZONE``. A 09:00 trial is still "today"
at 20:00; it is over at 00:00 the next morning.

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
    Rows whose trial day has not ended: no event_date at all, or an event_date
    on or after the start of today. A trial later today therefore stays in.
    """
    return (
        Q(event_date__isnull=True)
        | Q(event_date__gte=start_of_today(now))
    )


def is_trial_over(event_date, now=None):
    """The same rule for one row. No event_date → never over."""
    if event_date is None:
        return False
    return event_date < start_of_today(now)
