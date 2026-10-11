"""
The login-time Places call: queued, and DROPPED rather than run inline.

WHAT THIS IS ABOUT. ``on_successful_login`` used to call
``ensure_fresh_for_user`` directly, which for a user whose coordinates had
expired meant an HTTPS call to Google Places — up to 4 seconds — sitting
between a correct password and a token. Nobody was waiting on the result: the
coordinates only decide whether this player appears in somebody else's nearby
search.

``fallback="skip"`` IS THE WHOLE POINT AND IT IS THE ONLY SKIP IN THE CODEBASE.
enqueue's default would run the job inline whenever Celery is off or the broker
is in cooldown — which would put those 4 seconds straight back into the login
request, on exactly the days the infrastructure is already unhealthy. So when
the job cannot be queued it is dropped, and the next login (or the nightly
sweep) picks the work up.
"""

import uuid
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings

from apps.accounts.models import User, UserProfile
from apps.accounts.services.login_service import on_successful_login
from apps.legal.testing import accept_current_terms
from apps.places.tasks import refresh_user_location
from utils import background_jobs

ENSURE = "apps.places.tasks.ensure_fresh_for_user"

LOCMEM = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "places-login-tests",
    }
}


@override_settings(CACHES=LOCMEM)
class LoginCoordsRefreshTests(TestCase):

    def setUp(self):
        cache.clear()
        background_jobs.reset_dispatch_state()

        self.user = User.objects.create_user(
            email="player@example.com", password="pass1234", username="player",
        )
        accept_current_terms(self.user)
        UserProfile.objects.create(user=self.user, name="Player")

    # ── the dispatch ─────────────────────────────────────────────

    @override_settings(CELERY_ENABLED=True)
    def test_the_user_id_is_queued_with_the_skip_fallback(self):
        with patch("apps.accounts.services.login_service.enqueue") as enqueue:
            on_successful_login(self.user)

        enqueue.assert_called_once()
        task, args = enqueue.call_args.args
        self.assertEqual(task.name, "places.refresh_user_location")
        self.assertEqual(args, (str(self.user.id),))
        self.assertEqual(enqueue.call_args.kwargs["fallback"], "skip")

    @override_settings(CELERY_ENABLED=False)
    def test_with_celery_off_google_is_not_called_at_all(self):
        # NOT inline. A login must never wait on Google.
        with patch(ENSURE) as ensure:
            with self.assertLogs("utils.background_jobs", level="INFO") as logs:
                with self.captureOnCommitCallbacks(execute=True):
                    on_successful_login(self.user)

        ensure.assert_not_called()
        self.assertTrue(any("skipped" in line for line in logs.output))

    @override_settings(CELERY_ENABLED=True, CELERY_DISPATCH_FAILURE_COOLDOWN=60)
    def test_with_the_broker_in_cooldown_google_is_still_not_called(self):
        """
        The case the skip exists for. The broker has just failed a publish, so
        every dispatch in this process goes straight to the fallback for the
        next minute — and inline here would mean a 4-second Google call inside
        every login for that minute.
        """
        from kombu.exceptions import OperationalError

        with patch.object(
            refresh_user_location, "apply_async",
            side_effect=OperationalError("broker gone"),
        ):
            with patch(ENSURE) as ensure:
                with self.assertLogs("utils.background_jobs", level="ERROR"):
                    with self.captureOnCommitCallbacks(execute=True):
                        on_successful_login(self.user)   # the failed publish

                # In cooldown now: this one does not even try the broker.
                with self.captureOnCommitCallbacks(execute=True):
                    on_successful_login(self.user)

        ensure.assert_not_called()

    def test_last_login_is_still_stamped_when_the_refresh_is_skipped(self):
        # The two steps are isolated: the half that matters to the product
        # must not depend on the half that talks to Google.
        self.assertIsNone(self.user.last_login)

        with patch(ENSURE):
            on_successful_login(self.user)

        self.user.refresh_from_db()
        self.assertIsNotNone(self.user.last_login)

    def test_a_broken_places_stack_cannot_break_a_login(self):
        # on_successful_login's contract: never raises. The caller has already
        # authenticated the user and minted their tokens.
        with patch(
            "apps.accounts.services.login_service.enqueue",
            side_effect=ImportError("places is broken"),
        ):
            with self.assertLogs("apps.accounts.services.login_service",
                                 level="WARNING"):
                on_successful_login(self.user)


class RefreshUserLocationTaskTests(TestCase):

    def test_a_missing_user_returns_quietly(self):
        with patch(ENSURE) as ensure:
            with self.assertLogs("apps.places.tasks", level="INFO") as logs:
                result = refresh_user_location(str(uuid.uuid4()))

        self.assertIsNone(result)
        ensure.assert_not_called()
        self.assertIn("user gone", logs.output[0])

    def test_the_loaded_user_is_handed_to_the_service(self):
        user = User.objects.create_user(
            email="x@example.com", password="pass1234", username="x",
        )
        accept_current_terms(user)
        UserProfile.objects.create(user=user, name="X")

        with patch(ENSURE, return_value=True) as ensure:
            refresh_user_location(str(user.id))

        ensure.assert_called_once()
        self.assertEqual(ensure.call_args.args[0].id, user.id)

    def test_acks_late_is_off_because_a_lookup_costs_money(self):
        # A redelivery would buy the same metered Google call twice.
        self.assertFalse(refresh_user_location.acks_late)
