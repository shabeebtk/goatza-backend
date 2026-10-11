# places/tasks.py
"""
Celery tasks for places.

THIN, per CLAUDE.md ("Background jobs"): the task takes an id, loads the row
and calls the existing service, so the same top-up runs from a login, the
nightly ``refresh_place_coords`` command or a worker.
"""

import logging

from celery import shared_task
from django.core.management import call_command

from apps.accounts.models import User
from apps.places.services.coords_refresh_service import ensure_fresh_for_user

logger = logging.getLogger(__name__)


@shared_task(name="places.refresh_user_location", acks_late=False)
def refresh_user_location(user_id):
    """
    Top up the coordinates of the city on one user's profile.

    THIS USED TO RUN INSIDE THE LOGIN REQUEST. ``on_successful_login`` called
    ``ensure_fresh_for_user`` directly, which for a user whose coordinates had
    expired meant an HTTPS call to Google Places \u2014 up to 4 seconds \u2014 between
    a correct password and a token. Nobody was waiting on the result: the
    coordinates decide whether the player shows up in somebody else's nearby
    search, which is nothing the login response carries.

    ``acks_late=False``, inverting the project default, for a reason the push
    and email tasks do not have: a Google Places lookup COSTS MONEY and is
    metered against a daily cap (apps/places/services/places_service.py). A
    worker dying between the call and the ack would buy the same lookup twice
    on redelivery. At most once, and a lost refresh is handled \u2014 the next
    login tries again, and the nightly ``refresh_place_coords`` command sweeps
    whatever is still stale.

    Takes the user id as a string; the serializer is JSON.
    """
    user = (
        User.objects
        # ensure_fresh_for_user reads user.profile.location and nothing else.
        .select_related("profile__location")
        .filter(id=user_id)
        .first()
    )

    # Deleted between the login and the worker picking this up. Vanishingly
    # rare and in no way an error.
    if user is None:
        logger.info(
            "places.refresh_user_location | user gone, nothing to do | id=%s",
            user_id,
        )
        return

    # Never raises by contract (see its docstring); returns True only when
    # coordinates were actually written.
    refreshed = ensure_fresh_for_user(user)

    logger.info(
        "places.refresh_user_location | done | id=%s | refreshed=%s",
        user_id, refreshed,
    )


@shared_task(name="places.refresh_place_coords", acks_late=True)
def refresh_place_coords(limit=None):
    """
    Run the nightly coordinate lifecycle: refresh active places whose
    coordinates are stale, expire them for inactive ones.

    Explicit ``name=`` rather than the path-derived default, for the reason in
    core/celery.py: a task sitting in a queue under a path-derived name is
    undeliverable after any module move.

    SAFE TO RUN TWICE, which ``acks_late=True`` makes routine rather than
    exceptional. The command commits each location as it handles it and selects
    on ``coords_fetched_at``, so a redelivered run finds the rows it already
    refreshed no longer stale and a half-finished run simply leaves the rest
    for next time \u2014 its own docstring says so ("Safe to interrupt and safe to
    re-run").

    ``limit`` CAPS THE GOOGLE SPEND and is NOT set by the beat entry \u2014 the
    nightly run is meant to clear the whole backlog, and the Places daily
    Details cap (``PLACES_DAILY_CAP_DETAILS``, 1000) is what bounds it. It is
    here so an operator can enqueue a deliberately small run by hand, the same
    way ``recruitments.dispatch_announcements`` takes one.

    Takes a plain int or None, never a model instance \u2014 the serializer is JSON.
    """
    kwargs = {}
    if limit is not None:
        kwargs["limit"] = int(limit)

    call_command("refresh_place_coords", **kwargs)
