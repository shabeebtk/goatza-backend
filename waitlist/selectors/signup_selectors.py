"""
Read queries for the waitlist, and the one place the public numbers are made.

Two callers, two very different shapes of traffic:

  * ``signup_count`` backs the landing page's counter, which every visitor
    loads and nobody writes. It is cached for a minute so an Instagram story
    driving a few thousand taps costs one COUNT, not a few thousand.

  * ``get_by_ref_code`` backs the share card, which is one row by a short code
    somebody typed or tapped from a link.

THE DISPLAY OFFSET LIVES HERE, AND ONLY HERE.

``signup_number`` in the database is honest: a dense sequence from 1. What the
public is shown is that number plus ``WAITLIST_DISPLAY_OFFSET``, so the first
real player reads as #37 and an empty list never looks empty. Every caller that
shows a number to a human — the create response, the stats endpoint, the share
card, the honeypot decoy, the admin — goes through ``display_number`` or
``display_count``. That is the entire reason they are functions rather than a
``+ offset`` written out at each call site: three numbers derived three times
are three numbers that will eventually disagree, and the one thing the counter
and the card must never do is contradict each other in the same screenshot.
"""

from django.conf import settings

from waitlist.models import PlayerSignup
from utils.cache import cache_delete, cache_get, cache_set
from utils.cache_keys import CacheKeys

# Fallbacks for a settings module that predates either name. Mirrors the view's
# DEFAULT_WAITLIST_GOAL rather than importing it — a selector importing a view
# to read a constant is the wrong direction.
DEFAULT_DISPLAY_OFFSET = 36
DEFAULT_WAITLIST_GOAL = 1000

# Short on purpose. The number is the social proof on the page ("412 players
# already joined"), and it moves in bursts — a minute stale is invisible, an
# hour stale is a page that looks dead during the exact window it is working.
# The service busts the key on create anyway; this is the ceiling, not the plan.
SIGNUP_COUNT_TTL = 60


def signup_count() -> int:
    """
    How many players are on the list. Cached for ``SIGNUP_COUNT_TTL`` seconds.

    Returns the live count on a cache miss and warms the key on the way out.
    """
    cache_key = CacheKeys.waitlist_signup_count()

    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    count = PlayerSignup.objects.count()
    cache_set(cache_key, count, timeout=SIGNUP_COUNT_TTL)

    return count


def bust_signup_count():
    """
    Drop the cached counter. Called by PlayerSignupService after every create
    so the number on the success screen and the number on the landing page
    behind it are the same number.
    """
    cache_delete(CacheKeys.waitlist_signup_count())


def get_by_ref_code(code):
    """
    One signup by its public code ("GZ0413"), or None.

    Case-insensitive: the code is short enough to be typed by hand off a story
    screenshot, and "gz0413" is the same person. Codes are generated uppercase,
    so this only ever widens what already matches.
    """
    if not code:
        return None

    return (
        PlayerSignup.objects
        .filter(ref_code__iexact=str(code).strip())
        .first()
    )


# =====================================================================
# THE PUBLIC NUMBERS
# =====================================================================


def display_offset() -> int:
    """
    The head start the public numbers carry. Presentation only.

    Read from settings on every call rather than captured at import: a change
    to the environment should take effect on the next request, not on the next
    deploy. It is one integer lookup against an already-loaded module.
    """
    return int(getattr(settings, "WAITLIST_DISPLAY_OFFSET", DEFAULT_DISPLAY_OFFSET))


def display_number(signup_number) -> int:
    """
    The number a human is shown for a stored ``signup_number``.

    Signup 1 reads as #37 with the default offset of 36. Never inverted: no
    caller turns a display number back into a row, because ``ref_code`` is
    already the public handle for that and it is stored, not computed.
    """
    return int(signup_number or 0) + display_offset()


def display_count() -> int:
    """
    The counter on the landing page — ``signup_count`` plus the offset.

    Note this is deliberately the same arithmetic as ``display_number``, so the
    Nth player joining sees "#N+offset" on their card and "N+offset players"
    on the page behind it.
    """
    return signup_count() + display_offset()


def founding_cutoff() -> int:
    """
    The highest DISPLAY number that still counts as a founding player.

    The goal doubles as the cutoff: the cohort is "everybody who got in before
    we hit the number on the progress bar", which is the promise the landing
    page already makes.
    """
    return int(getattr(settings, "WAITLIST_GOAL", DEFAULT_WAITLIST_GOAL))


def is_founding(signup_number) -> bool:
    """
    Whether a stored ``signup_number`` belongs to the founding cohort.

    Measured on the DISPLAY number, not the stored one — the badge has to agree
    with the number printed next to it on the same card.

    The offset therefore eats into the cohort: with an offset of 36 and a goal
    of 1000, the founding players are display numbers 37..1000, which is 964
    real signups. That is intended. A player who is shown #1000 and told they
    are the last founding member must not later be told otherwise because the
    database privately called them #964.
    """
    return display_number(signup_number) <= founding_cutoff()
