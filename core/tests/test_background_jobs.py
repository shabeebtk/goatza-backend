"""
utils.background_jobs.enqueue — the one dispatch path for background jobs.

Every test here passes with NO Redis and NO worker. That is the property under
test: with CELERY_ENABLED off the helper never touches a broker, and with it on
a broker that fails is a log line, not an exception in a request.

Celery reads its configuration once, when the app is first configured, so
``override_settings`` cannot swap the broker under a test. Broker behaviour is
therefore patched at ``task.apply_async``; ``override_settings`` is used only
for ``CELERY_ENABLED`` and the cooldown, which the helper reads at call time.

The one test that talks to a real (blackholed) broker is opt-in via
``RUN_SLOW_CELERY_TESTS=1`` — it costs a couple of seconds by design.
"""

import os
import time
from unittest import skipUnless
from unittest.mock import patch

from django.db import transaction
from django.test import TestCase, override_settings
from kombu.exceptions import OperationalError

from core.celery import app as celery_app
from utils import background_jobs
from utils.background_jobs import enqueue

# What the throwaway task appends to. Inline execution is observable here and
# nowhere else — a queued task never touches it.
RAN = []


@celery_app.task(name="core.tests.background_jobs.record")
def record(*args, **kwargs):
    RAN.append((args, kwargs))


@celery_app.task(name="core.tests.background_jobs.explode")
def explode():
    raise RuntimeError("task blew up")


class BackgroundJobsTests(TestCase):

    def setUp(self):
        RAN.clear()
        background_jobs.reset_dispatch_state()

    # =================================================================
    # CELERY OFF (the default everywhere until a worker exists)
    # =================================================================

    @override_settings(CELERY_ENABLED=False)
    def test_disabled_runs_inline_and_never_touches_the_broker(self):
        with patch.object(record, "apply_async") as apply_async:
            enqueue(record, args=(1,), kwargs={"x": 2}, on_commit=False)

        apply_async.assert_not_called()
        self.assertEqual(RAN, [((1,), {"x": 2})])

    @override_settings(CELERY_ENABLED=False)
    def test_disabled_with_skip_fallback_runs_nothing(self):
        with patch.object(record, "apply_async") as apply_async:
            enqueue(record, args=(1,), fallback="skip", on_commit=False)

        apply_async.assert_not_called()
        self.assertEqual(RAN, [])

    @override_settings(CELERY_ENABLED=False)
    def test_disabled_reports_the_path_it_took(self):
        self.assertEqual(background_jobs._dispatch(record), "inline")
        self.assertEqual(
            background_jobs._dispatch(record, fallback="skip"), "skipped"
        )

    @override_settings(CELERY_ENABLED=False)
    def test_a_raising_inline_task_is_logged_and_swallowed(self):
        with self.assertLogs("utils.background_jobs", level="ERROR") as logs:
            result = background_jobs._dispatch(explode)

        self.assertEqual(result, "failed")
        self.assertIn("inline run raised", logs.output[0])

    @override_settings(CELERY_ENABLED=False)
    def test_a_dropped_delay_is_logged(self):
        with self.assertLogs("utils.background_jobs", level="INFO") as logs:
            enqueue(record, countdown=30, on_commit=False)

        self.assertTrue(any("delay dropped" in line for line in logs.output))
        self.assertEqual(len(RAN), 1)

    # =================================================================
    # CELERY ON, BROKER HEALTHY
    # =================================================================

    @override_settings(CELERY_ENABLED=True)
    def test_enabled_publishes_once_with_the_given_arguments(self):
        with patch.object(record, "apply_async") as apply_async:
            enqueue(record, args=(1,), kwargs={"x": 2}, countdown=5, on_commit=False)

        apply_async.assert_called_once_with(args=(1,), kwargs={"x": 2}, countdown=5)
        # Queued means handed to the broker — it must NOT also have run here.
        self.assertEqual(RAN, [])

    @override_settings(CELERY_ENABLED=True)
    def test_enabled_reports_queued(self):
        with patch.object(record, "apply_async"):
            self.assertEqual(background_jobs._dispatch(record), "queued")

    # =================================================================
    # CELERY ON, BROKER DOWN
    # =================================================================

    @override_settings(CELERY_ENABLED=True)
    def test_a_publish_failure_falls_back_inline_and_logs_an_error(self):
        with patch.object(
            record, "apply_async", side_effect=OperationalError("broker gone")
        ):
            with self.assertLogs("utils.background_jobs", level="ERROR") as logs:
                # No exception may escape — the request this is a side effect
                # of has already done its work.
                enqueue(record, args=("a",), on_commit=False)

        self.assertEqual(RAN, [(("a",), {})])
        self.assertIn("publish FAILED", logs.output[0])

    @override_settings(CELERY_ENABLED=True, CELERY_DISPATCH_FAILURE_COOLDOWN=60)
    def test_inside_the_cooldown_the_broker_is_not_tried_again(self):
        with patch.object(
            record, "apply_async", side_effect=OperationalError("broker gone")
        ) as apply_async:
            enqueue(record, args=(1,), on_commit=False)
            enqueue(record, args=(2,), on_commit=False)

        # Only the FIRST call paid for the broker; the second went straight
        # to the fallback and both ran.
        self.assertEqual(apply_async.call_count, 1)
        self.assertEqual(RAN, [((1,), {}), ((2,), {})])

    @override_settings(CELERY_ENABLED=True, CELERY_DISPATCH_FAILURE_COOLDOWN=60)
    def test_after_the_cooldown_the_broker_is_tried_again(self):
        now = [1000.0]
        with patch.object(background_jobs, "_clock", lambda: now[0]):
            with patch.object(
                record, "apply_async", side_effect=OperationalError("broker gone")
            ) as apply_async:
                enqueue(record, on_commit=False)
                now[0] += 59
                enqueue(record, on_commit=False)
                self.assertEqual(apply_async.call_count, 1)

                now[0] += 2  # 61s after the failure
                enqueue(record, on_commit=False)
                self.assertEqual(apply_async.call_count, 2)

    @override_settings(CELERY_ENABLED=True)
    def test_a_publish_failure_with_skip_fallback_runs_nothing(self):
        with patch.object(
            record, "apply_async", side_effect=OperationalError("broker gone")
        ):
            with self.assertLogs("utils.background_jobs", level="ERROR"):
                enqueue(record, fallback="skip", on_commit=False)

        self.assertEqual(RAN, [])

    # =================================================================
    # TRANSACTION BOUNDARY
    # =================================================================

    @override_settings(CELERY_ENABLED=False)
    def test_default_dispatch_waits_for_commit(self):
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            enqueue(record, args=(1,))
            # Registered, not run: the row this job would read may not be
            # committed yet.
            self.assertEqual(RAN, [])

        self.assertEqual(len(callbacks), 1)
        callbacks[0]()
        self.assertEqual(RAN, [((1,), {})])

    @override_settings(CELERY_ENABLED=False)
    def test_a_rollback_dispatches_nothing(self):
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            with self.assertRaises(RuntimeError):
                with transaction.atomic():
                    enqueue(record, args=(1,))
                    raise RuntimeError("request failed after enqueue")

        self.assertEqual(callbacks, [])
        self.assertEqual(RAN, [])

    @override_settings(CELERY_ENABLED=False)
    def test_on_commit_false_dispatches_immediately(self):
        with transaction.atomic():
            enqueue(record, args=(1,), on_commit=False)
            # Still inside the transaction and it has already run.
            self.assertEqual(RAN, [((1,), {})])

    def test_an_unknown_fallback_is_a_programming_error(self):
        with self.assertRaises(ValueError):
            enqueue(record, fallback="retry")

    # =================================================================
    # REGRESSION GUARD
    # =================================================================

    def test_healthz_does_not_report_on_celery(self):
        # A dead worker must never make /healthz degraded — Render would
        # recycle the WEB service for it. The body keys are the contract.
        res = self.client.get("/healthz")

        self.assertEqual(res.status_code, 200)
        self.assertEqual(set(res.json().keys()), {"status", "db", "redis"})


@skipUnless(
    os.getenv("RUN_SLOW_CELERY_TESTS") == "1",
    "opt-in: RUN_SLOW_CELERY_TESTS=1 (talks to a blackholed broker, ~2s)",
)
class BlackholedBrokerTests(TestCase):
    """
    The number behind CELERY_BROKER_TRANSPORT_OPTIONS. With Celery's defaults a
    publish to an unreachable Redis blocks for 100+ seconds; with the transport
    options in settings it fails in about two. This test builds its own Celery
    app because the project app's broker is fixed at configure time.
    """

    def test_a_blackholed_broker_fails_fast_and_falls_back(self):
        from celery import Celery
        from django.conf import settings

        blackhole = Celery("blackhole", broker="redis://10.255.255.1:6379/0")
        blackhole.conf.update(
            broker_transport_options=settings.CELERY_BROKER_TRANSPORT_OPTIONS,
            task_publish_retry_policy=settings.CELERY_TASK_PUBLISH_RETRY_POLICY,
            task_ignore_result=True,
            task_always_eager=False,
        )

        @blackhole.task(name="core.tests.background_jobs.blackholed")
        def blackholed():
            RAN.append("blackholed")

        RAN.clear()
        background_jobs.reset_dispatch_state()

        started = time.monotonic()
        with override_settings(CELERY_ENABLED=True):
            with self.assertLogs("utils.background_jobs", level="ERROR"):
                enqueue(blackholed, on_commit=False)
        elapsed = time.monotonic() - started

        print(f"\nblackholed broker publish gave up after {elapsed:.2f}s")
        self.assertLess(elapsed, 3.0)
        self.assertEqual(RAN, ["blackholed"])
