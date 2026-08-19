"""
Read queries for the waitlist.

Two callers, two very different shapes of traffic:

  * ``signup_count`` backs the landing page's counter, which every visitor
    loads and nobody writes. It is cached for a minute so an Instagram story
    driving a few thousand taps costs one COUNT, not a few thousand.

  * ``get_by_ref_code`` backs the share card, which is one row by a short code
    somebody typed or tapped from a link.
"""

from waitlist.models import PlayerSignup
from utils.cache import cache_delete, cache_get, cache_set
from utils.cache_keys import CacheKeys

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
