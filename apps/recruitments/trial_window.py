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

And one rule about a SINGLE DATE rather than the whole window:

  * ``live_session_q``    — the centres that still count, for nearest-centre
                            distance and the city filter

Kept in its own module so ``models.py`` and the selectors can share it without
either importing the other. ``now`` is injectable everywhere for tests.
"""

from datetime import timedelta

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


# ---------------------------------------------------------------------
# ONE DATE, not the window
# ---------------------------------------------------------------------
#
# The rules above are about the WHOLE trial and read a stored instant, so they
# need no timezone. A single ``TrialSession`` has no such instant: it stores a
# bare ``date``, and the clock it belongs to lives on its parent recruitment.
# Nearest-centre distance needs the per-date answer anyway — a Kochi date that
# already ran must stop making a trial "near Kochi" — and it needs it INSIDE a
# subquery, where there is no Python to fall back to.

# The furthest a venue's clock can sit BEHIND UTC. tzdata's westernmost zone
# is Etc/GMT+12, so a calendar day is over everywhere on Earth twelve hours
# after it is over in UTC.
MAX_HOURS_BEHIND_UTC = 12


def earliest_live_session_date(now=None):
    """
    The earliest session ``date`` whose day may still be running SOMEWHERE,
    as a plain date for a ``date__gte`` comparison.

    DELIBERATELY NOT EXACT, and the direction matters. Being exact would mean
    reading each row's venue clock in SQL — ``timezone(recruitment.timezone,
    now())`` — and this codebase refuses that on purpose, in two places that
    say so: the module docstring above ("making the read timezone-aware would
    mean converting zones in SQL"), and ``utils.timezones`` ("Nothing queries
    with a timezone"). There is a second, harder reason. ``utils.timezones``
    answers an unknown zone name with the default rather than raising,
    because "answering with the default beats raising on a list endpoint" —
    and Postgres has no such courtesy. One row whose stored name tzdata later
    dropped would turn every "near me" query into a 500.

    So the comparison errs in the ONE safe direction: a date stays live until
    its day is over at the westernmost clock on Earth. A centre is never
    dropped while it is still today at its own venue, which is the error that
    would actually be visible — a trial vanishing from "near me" on the
    morning it runs.

    THE COST, stated plainly: a date that has finished keeps counting for up
    to twelve hours longer than its own midnight, and 17.5 hours for an
    Asia/Kolkata venue. What that buys during the overlap is a stale DISTANCE
    (a just-finished Kochi centre instead of next week's Kannur one) on a
    trial that is still live and still listed either way. The discover payload
    is already cached for ten minutes against the same class of drift.

    If that ever stops being acceptable, the fix is the one the module
    recommends everywhere else: resolve the instant on the way IN — a stored
    ``ends_at`` per session, written by ``_sync_trial_sessions`` the way
    ``trial_end_date`` already is — not a timezone conversion on the way out.
    """
    now = now or timezone.now()
    return (now - timedelta(hours=MAX_HOURS_BEHIND_UTC)).date()


def live_session_q(now=None, prefix=""):
    """
    The centres that still COUNT: not cancelled, and their day not yet over.

    ONE definition, three readers — the nearest-centre distance subquery, the
    distance box prefilter and the city filter. A centre that matched one and
    not the others would mean a trial that is "within 50 km" but cannot be
    found by the city it is 50 km from.

    ``prefix`` is the path from the model being queried to the session: ""
    when the queryset is over ``TrialSession`` itself, ``"sessions__"`` when
    it is over ``Recruitment`` and reaches them through the reverse relation.
    Combine it into a SINGLE ``.filter()`` call with whatever else the caller
    asks of the same session — chaining a second ``.filter()`` would join the
    relation twice and let two different rows satisfy the two halves.

    Says nothing about coordinates: "is this centre still on" and "do we know
    where it is" are different questions, and the distance callers add the
    second themselves.
    """
    return Q(**{
        f"{prefix}is_cancelled": False,
        f"{prefix}date__gte": earliest_live_session_date(now),
    })
