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
from datetime import timedelta
from unittest import skipUnless
from unittest.mock import MagicMock, patch

from django.core.cache import cache
from django.db import transaction
from django.test import TestCase, override_settings
from django.utils import timezone
from kombu.exceptions import OperationalError

from core.celery import app as celery_app
from core.celery import heartbeat
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

    def test_healthz_reports_the_worker_but_never_acts_on_it(self):
        """
        /healthz NAMES the worker and never lets it change the verdict.

        This test used to assert the opposite — that the body had no Celery
        key at all — which was right while nothing ran on Celery. The worker
        field was added once a dead worker became possible, and the rule it
        was protecting is unchanged and still the point: Render recycles a
        container that answers non-200 here, and recycling the WEB service
        would not bring a dead WORKER back. So the field is reported, and
        ``status`` and the status code still come from Postgres alone.
        """
        res = self.client.get("/healthz")
        body = res.json()

        self.assertEqual(res.status_code, 200)
        self.assertEqual(set(body.keys()), {"status", "db", "redis", "worker"})
        self.assertIn(body["worker"], {"ok", "stale", "unknown"})


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


# A cache that actually round-trips. The project uses ResilientRedisCache
# whenever REDIS_URL is set, and with no Redis listening its breaker answers
# every read with the caller's default — which would make worker_state() say
# "unknown" no matter what these tests wrote. LocMemCache makes the heartbeat
# observable and the result independent of the developer's environment.
LOCMEM = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "worker-heartbeat-tests",
    }
}


@override_settings(CACHES=LOCMEM, CELERY_WORKER_STALE_AFTER=180)
class WorkerStateTests(TestCase):
    """
    ``worker_state()`` — "is anybody draining the queue?"

    The asymmetry is the whole design and every test here is about it: only a
    heartbeat that is PRESENT AND OLD may answer "stale", because "stale" is
    what makes _dispatch stop using the broker. A missing key is what a fresh
    deploy, a flushed Redis and a degraded cache all look like, and inlining
    every job in the fleet on any of those would be an outage of its own.
    """

    def setUp(self):
        cache.clear()
        # Clears the publish-failure cooldown AND the worker-stale log
        # timestamp the heartbeat work added.
        background_jobs.reset_dispatch_state()

    def _write_heartbeat(self, age_seconds=0):
        cache.set(
            background_jobs.WORKER_LAST_SEEN_KEY,
            (timezone.now() - timedelta(seconds=age_seconds)).isoformat(),
            timeout=None,
        )

    # ── the four answers ─────────────────────────────────────────

    def test_no_key_is_unknown(self):
        self.assertEqual(background_jobs.worker_state(), "unknown")

    def test_a_fresh_heartbeat_is_ok(self):
        # Through the real task, so the format it writes is the format read.
        heartbeat()

        self.assertEqual(background_jobs.worker_state(), "ok")

    def test_a_heartbeat_inside_the_threshold_is_still_ok(self):
        self._write_heartbeat(age_seconds=179)

        self.assertEqual(background_jobs.worker_state(), "ok")

    def test_an_old_heartbeat_is_stale(self):
        self._write_heartbeat(age_seconds=400)

        self.assertEqual(background_jobs.worker_state(), "stale")

    def test_an_unparseable_value_is_unknown_not_stale(self):
        # Somebody else's value under our key, or a format change mid-deploy.
        # Guessing "stale" here would inline every job over a bad string.
        cache.set(background_jobs.WORKER_LAST_SEEN_KEY, "yesterday", timeout=None)

        with self.assertLogs("utils.background_jobs", level="WARNING"):
            self.assertEqual(background_jobs.worker_state(), "unknown")

    def test_a_cache_that_raises_is_unknown(self):
        broken = MagicMock()
        broken.get.side_effect = ConnectionError("redis is gone")

        with patch.object(background_jobs, "cache", broken):
            with self.assertLogs("utils.background_jobs", level="WARNING"):
                self.assertEqual(background_jobs.worker_state(), "unknown")

    def test_a_clock_skewed_heartbeat_from_the_future_is_ok(self):
        # Negative age. The web and worker containers are separate clocks, and
        # the safe direction for skew is "ok" — never a fleet-wide inline.
        self._write_heartbeat(age_seconds=-30)

        self.assertEqual(background_jobs.worker_state(), "ok")

    # ── the heartbeat task itself ────────────────────────────────

    def test_the_heartbeat_is_written_with_no_expiry(self):
        # A key that expired on its own would read as "missing" → "unknown" →
        # healthy, minutes after the worker died. The lingering value IS the
        # signal, so the TTL must be None.
        with patch("django.core.cache.cache") as mock_cache:
            heartbeat()

        self.assertEqual(mock_cache.set.call_args.kwargs["timeout"], None)

    def test_the_heartbeat_swallows_a_broken_cache(self):
        broken = MagicMock()
        broken.set.side_effect = ConnectionError("redis is gone")

        with patch("django.core.cache.cache", broken):
            with self.assertLogs("core.celery", level="WARNING"):
                # A worker must not crash on its own liveness probe.
                heartbeat()


@override_settings(CACHES=LOCMEM, CELERY_WORKER_STALE_AFTER=180)
class WorkerStaleDispatchTests(TestCase):
    """
    What a stale worker does to a dispatch: it stops being queued.

    This is the one failure a successful ``apply_async`` cannot reveal — the
    broker accepts the publish, the request succeeds, and the job sits in a
    list nobody is draining.
    """

    def setUp(self):
        RAN.clear()
        cache.clear()
        background_jobs.reset_dispatch_state()

    def _heartbeat(self, age_seconds):
        cache.set(
            background_jobs.WORKER_LAST_SEEN_KEY,
            (timezone.now() - timedelta(seconds=age_seconds)).isoformat(),
            timeout=None,
        )

    @override_settings(CELERY_ENABLED=True)
    def test_a_stale_worker_runs_inline_and_does_not_publish(self):
        self._heartbeat(age_seconds=400)

        with patch.object(record, "apply_async") as apply_async:
            with self.assertLogs("utils.background_jobs", level="ERROR") as logs:
                result = background_jobs._dispatch(record, args=(1,))

        apply_async.assert_not_called()
        self.assertEqual(result, "inline")
        self.assertEqual(RAN, [((1,), {})])
        self.assertTrue(any("WORKER STALE" in line for line in logs.output))

    @override_settings(CELERY_ENABLED=True)
    def test_an_unknown_worker_still_publishes(self):
        # No key. Nothing is known to be wrong, so the job is queued — a
        # publish that then fails is what the inline fallback is for.
        with patch.object(record, "apply_async") as apply_async:
            result = background_jobs._dispatch(record, args=(1,))

        apply_async.assert_called_once()
        self.assertEqual(result, "queued")
        self.assertEqual(RAN, [])

    @override_settings(CELERY_ENABLED=True)
    def test_a_fresh_heartbeat_publishes(self):
        heartbeat()

        with patch.object(record, "apply_async") as apply_async:
            self.assertEqual(background_jobs._dispatch(record), "queued")

        apply_async.assert_called_once()

    @override_settings(CELERY_ENABLED=True, CELERY_DISPATCH_FAILURE_COOLDOWN=60)
    def test_the_stale_error_is_logged_once_per_cooldown_window(self):
        # A dead worker is rediscovered by every single dispatch, and the line
        # reporting it is at ERROR so Sentry raises an event. Unthrottled that
        # is one event per job for the length of the outage.
        self._heartbeat(age_seconds=400)

        with patch.object(record, "apply_async"):
            with self.assertLogs("utils.background_jobs", level="DEBUG") as logs:
                for _ in range(4):
                    background_jobs._dispatch(record)

        errors = [line for line in logs.output if line.startswith("ERROR")]
        repeats = [line for line in logs.output if "worker still stale" in line]

        self.assertEqual(len(errors), 1)
        self.assertEqual(len(repeats), 3)
        self.assertEqual(len(RAN), 4)   # every job still ran

    @override_settings(CELERY_ENABLED=False)
    def test_with_celery_off_the_heartbeat_is_never_consulted(self):
        # The enabled check comes first, so the default configuration pays no
        # cache read per job.
        self._heartbeat(age_seconds=400)

        with patch.object(
            background_jobs, "worker_state", side_effect=AssertionError
        ):
            background_jobs._dispatch(record)

        self.assertEqual(len(RAN), 1)
