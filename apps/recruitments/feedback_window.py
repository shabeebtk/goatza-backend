# recruitments/feedback_window.py
"""
THE rule for when a player may be asked how their trial went.

ONE HOME, because the rule is answered in two places that must agree: the
endpoint that accepts an answer, and the ``can_give_feedback`` flag the client
reads to decide whether to ask. A client that re-derives "trial ended, and my
status is one of these three" from dates and statuses will get it subtly
different, and the result is a prompt that 400s the moment somebody taps it.

WHO. Only somebody the org actually CALLED to the trial:

    trial_confirmed   they were called, and nobody has posted a result
    selected          they were called, and they made it
    not_selected      they were called, and they did not

``not_shortlisted`` is deliberately absent, and so is everything before the
trial. Asking somebody who was never called in how the trial went is a bad
question and an unkind one.

WHEN. Only once the trial's LAST day has ended — ``trial_window.is_trial_over``
on the cached ``trial_end_date``, which is a STORED INSTANT that already
carries the day boundary for that trial's own country (resolved at write time
by ``_sync_trial_window``). So this is a plain UTC comparison with no timezone
in it, and a London player is asked at midnight London while an Indian one is
asked at midnight IST. A recruitment with no trial dates at all (a
``player_looking`` post) therefore never opens: it has no trial to report on.

TWO FLAGS, NOT ONE, and the client needs both:

  * ``can_give_feedback``  — this player may be ASKED: right status, trial
    over, and they have not answered yet.
  * ``feedback_window_open`` — the trial ended within the last
    ``PROMPT_DAYS``.

The client shows the prompt when BOTH are true. They are separate because the
ENDPOINT stays open indefinitely — a late answer is still a good answer, and a
player who finally hears back in March should be able to come and say so — but
a prompt that follows somebody around for a year is nagging, not asking.

RESUBMITTING is allowed and is NOT what these flags describe: ``waiting`` is a
real answer that becomes wrong later, so a player who has answered may still
POST again. ``can_give_feedback`` goes false the moment they answer, because
its job is "should we ask", not "would the server accept".
"""

from datetime import timedelta

from django.utils import timezone

from apps.recruitments.trial_window import is_trial_over

# Statuses that mean "the org called this player to the trial". Any other
# status answers 404 at the endpoint, the same way every ownership gate here
# refuses — never leaking that the application exists.
ELIGIBLE_STATUSES = frozenset({
    "trial_confirmed",
    "selected",
    "not_selected",
})

# How long after the trial's last day the client should keep offering the
# prompt. The endpoint ignores this entirely.
PROMPT_DAYS = 30


def status_allows_feedback(status):
    """Was this player called to the trial at all?"""
    return status in ELIGIBLE_STATUSES


def trial_is_over(recruitment, now=None):
    """
    Has the trial's last day ended? The shared rule, so a trial that ran this
    morning is not reportable until midnight AT ITS VENUE.
    """
    return is_trial_over(recruitment.trial_end_date, now=now)


def has_answered(application):
    """
    ``feedback_at`` is the single signal, and it is stamped on EVERY accepted
    answer — including "I did not attend", which clears the outcome and the
    rating and would otherwise read as no answer at all.
    """
    return application.feedback_at is not None


def prompt_window_open(recruitment, now=None):
    """
    The trial ended, and it ended within the last ``PROMPT_DAYS``. False both
    before the trial is over and long after.
    """
    if not trial_is_over(recruitment, now=now):
        return False

    end = recruitment.trial_end_date
    if end is None:
        return False

    now = now or timezone.now()
    return now - end <= timedelta(days=PROMPT_DAYS)


def can_give_feedback(application, recruitment=None, now=None):
    """
    Should this player be ASKED? Right status, trial over, not answered yet.

    ``recruitment`` is accepted so a caller holding the row already (the
    serializers all do, via select_related) does not walk the FK back.
    """
    recruitment = recruitment or application.recruitment

    return (
        status_allows_feedback(application.status)
        and trial_is_over(recruitment, now=now)
        and not has_answered(application)
    )
