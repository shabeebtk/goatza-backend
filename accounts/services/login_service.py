"""
What has to happen the moment a login succeeds, in one place.

Three views issue tokens — password, OTP verification and the Google OAuth
callback — and each of them used to just call ``RefreshToken.for_user`` and
return. That is the bug this module exists to close: ``for_user`` does NOT
write ``last_login``. SimpleJWT only updates it inside ``TokenObtainSerializer``
(which this repo does not use) and Django only updates it from the
``user_logged_in`` signal that ``django.contrib.auth.login`` sends (which these
API views never call), so ``UPDATE_LAST_LOGIN = True`` in settings has been
true and inert. Every user's ``last_login`` was NULL.

That matters now because docs/PLACES_MIGRATION.md section 6 makes
``last_login`` decide whether a profile's city keeps its coordinates.

Call ``on_successful_login(user)`` from every path that hands out a token for a
freshly authenticated user — and from none that hand one out for an already
authenticated one. A token refresh is not a login: it happens every fifteen
minutes in the background, so counting it would mark every dormant account as
active forever and turn the expiry into a no-op. Neither is the re-issue after
a password change — the caller was already signed in.
"""

import logging

from django.contrib.auth.models import update_last_login

logger = logging.getLogger(__name__)


def on_successful_login(user):
    """
    Stamp ``last_login`` and top up the user's city coordinates.

    **Never raises.** A login must not fail because of anything in here: the
    caller has already authenticated the user and already minted their tokens,
    so an exception at this point would turn a successful sign-in into a 500.
    Each step is isolated so a failure in one still lets the other run.

    Costs at most one Google call (4 s ceiling, and only for a user whose city
    coordinates are stale or expired — for almost every login, none).
    """
    TAG = "on_successful_login"

    try:
        update_last_login(None, user)
    except Exception as e:
        logger.warning(f"{TAG} | last_login not updated | {type(e).__name__}")

    try:
        # Imported inside the function so `accounts` carries no module-level
        # dependency on `places`. The dependency already runs the other way —
        # coords_refresh_service reaches into UserProfile — and an import error
        # in the Places stack must not be able to take the auth views down with
        # it. It is one cached module lookup per login.
        from places.services.coords_refresh_service import ensure_fresh_for_user

        ensure_fresh_for_user(user)
    except Exception as e:
        logger.warning(f"{TAG} | coords not refreshed | {type(e).__name__}")
