"""
Email OTP codes: minted here, stored ONLY in the cache, verified here.

WHY THIS FILE IS THE ONE EXCEPTION TO "THE CACHE MAY SILENTLY FAIL".

Everything else in the app treats the cache as an optimisation, and
core/cache/resilient.py makes a dead Redis behave like a permanent cache miss
so that nothing 500s. For an OTP that would be the worst possible outcome:
the code is mailed, verification reads back nothing, and the user is told
"Invalid or expired code" for a code they are holding and typed correctly.
That is not a degradation — it is an app that looks like broken auth, and it
generates support tickets instead of an alert.

So the OTP path checks that its write actually landed and fails LOUDLY:

  * the caller gets ``OTPStorageError`` and turns it into a 503 with
    ``OTP_UNAVAILABLE_MESSAGE`` — honest, actionable, and naming no
    infrastructure
  * nothing is emailed, because an email carrying a code that can never
    verify is worse than no email at all
  * it is logged at ERROR, so the outage reaches Sentry

VERIFICATION IS UNCHANGED, on purpose. A missing entry there already means
"invalid or expired", which is exactly right when the code genuinely expired,
and a ten-minute-old code is far more likely to have expired than to have been
lost to an outage.
"""

import logging
import random

from utils.cache import cache_delete, cache_get, cache_set
from utils.cache_keys import CacheKeys

logger = logging.getLogger(__name__)

OTP_EXPIRE_MINUTES = 10  # OTP valid for 10 minutes

# What the user is told. No "Redis", no "cache", no "storage" — they cannot act
# on any of that. "In a moment" is true: the backend's cooldown is 30s.
OTP_UNAVAILABLE_MESSAGE = (
    "We couldn't send your code right now. Please try again in a moment."
)


class OTPStorageError(RuntimeError):
    """
    The code could not be stored, so it could never have been verified.

    Deliberately NOT a DRF ``APIException``: DRF's handler would answer in its
    own ``{"detail": ...}`` shape, and every endpoint in this app answers in
    ``utils.response.response_data``'s shape. Each caller catches this and
    returns a 503 in the house format — see the callers of ``generate_otp``.
    """


def generate_otp(email: str, purpose: str = None) -> str:
    """
    Generate and store an OTP. Raises ``OTPStorageError`` if the write did not
    land, in which case the caller must NOT send an email.

    ``purpose`` is optional. Omitted (signup, login verification, forgot
    password) it keeps the shared per-address key those three have always used.
    Passed, the code lands under its own key and can only be spent by a flow
    asking for the same purpose — see CacheKeys.email_otp.
    """
    otp = str(random.randint(1001, 9999))
    key = CacheKeys.email_otp(email, purpose)

    cache_set(key, otp, timeout=OTP_EXPIRE_MINUTES * 60)

    # READ IT BACK, rather than asking the backend whether it is degraded.
    # The read back is the stronger check and the backend-agnostic one: it
    # catches a breaker that is open, a write the client buffered and never
    # delivered, and an eviction under memory pressure — and it works the same
    # on LocMemCache, where there is no breaker to ask.
    #
    # One extra round trip per OTP. Trivial next to the email that follows,
    # and this is the one write in the app that has no system of record behind
    # it.
    if cache_get(key) != otp:
        logger.error(
            "otp | code could not be stored, not sending mail | "
            "purpose=%s | key=%s",
            purpose or "default", key,
        )
        raise OTPStorageError(OTP_UNAVAILABLE_MESSAGE)

    return otp


def verify_otp(email: str, otp_input: str, purpose: str = None) -> bool:
    """
    Check OTP validity. ``purpose`` must match the one it was issued under.

    A missing entry is False — "invalid or expired" — and that stays correct
    whether the code expired or the cache lost it. See the module docstring for
    why this half is not made to fail loudly.
    """
    key = CacheKeys.email_otp(email, purpose)
    otp = cache_get(key)
    if otp and otp == otp_input:
        cache_delete(key)  # invalidate after successful verification
        return True

    return False
