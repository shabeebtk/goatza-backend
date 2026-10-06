"""
GET /healthz — the Render liveness probe.

The load-bearing test in this file is
``test_the_probe_is_not_throttled_by_the_anon_budget``: everything else can be
re-derived from the view, but the whole reason /healthz is a plain Django view
instead of a ``PublicAPIView`` is that DRF's 20/min anon throttle would start
answering 429 to a probe polling on a 10s interval — and Render reads a 429 as
"unhealthy" and recycles the instance. A test that only asserts one 200 would
pass just as happily against a throttled endpoint.

The failure cases patch the view's OWN references to ``cache`` and
``connection`` rather than the real backends: the point under test is that the
view degrades, not that Django's Redis client raises what we think it raises.

ONLY THE DATABASE DECIDES THE STATUS CODE. That changed when the cache became
resilient (core/cache/resilient.py): the app now serves without Redis, so
answering 503 over it would have Render recycle a container that is working,
and replace it with one equally unable to reach Redis. A degraded cache is
therefore a named component at 200. An unreachable Postgres is still a 503,
because without it this process can serve nothing.
"""

from unittest.mock import MagicMock, patch

from django.test import TestCase

HEALTH_URL = "/healthz"


class HealthzTests(TestCase):

    # =================================================================
    # HEALTHY
    # =================================================================

    def test_both_components_healthy_is_a_200(self):
        # The test database is real and the cache is LocMemCache under the test
        # settings, so this is the genuine both-up path.
        res = self.client.get(HEALTH_URL)

        self.assertEqual(res.status_code, 200)
        self.assertEqual(
            res.json(), {"status": "ok", "db": "ok", "redis": "ok"}
        )

    def test_the_probe_needs_no_authorization_header(self):
        # The whole point. A caller with no token, no cookie and no actor
        # headers must reach this — anything that makes it 401 makes Render
        # restart a perfectly healthy service.
        res = self.client.get(HEALTH_URL)

        self.assertEqual(res.status_code, 200)

    def test_the_probe_is_not_throttled_by_the_anon_budget(self):
        # DRF's 'anon' scope is 20/min. Render polls far above that rate, so
        # request 21 must still be a 200 — which it is only because this view
        # never enters DRF at all.
        statuses = {self.client.get(HEALTH_URL).status_code for _ in range(25)}

        self.assertEqual(statuses, {200})

    def test_the_response_is_not_cacheable(self):
        # A proxy or a CDN that cached a 200 would keep reporting "up" for the
        # life of the entry, which is exactly the outage nobody notices.
        res = self.client.get(HEALTH_URL)

        self.assertIn("no-cache", res.headers.get("Cache-Control", ""))

    # =================================================================
    # DEGRADED
    # =================================================================

    def test_a_broken_cache_is_a_200_naming_redis(self):
        """
        REPORTED, NOT ACTED ON. The body says the cache is degraded so a human
        or a dashboard can see it; the 200 is what stops Render killing a
        container that is still serving every request it is given.
        """
        broken = MagicMock()
        broken.set.side_effect = ConnectionError("redis is gone")

        with patch("core.views.health_views.cache", broken):
            res = self.client.get(HEALTH_URL)

        self.assertEqual(res.status_code, 200)
        self.assertEqual(
            res.json(), {"status": "degraded", "db": "ok", "redis": "degraded"}
        )

    def test_a_cache_that_loses_the_value_is_also_degraded(self):
        # The silent failure the round trip exists to catch: set() returns
        # cleanly, the value never comes back. No exception is raised anywhere,
        # so the backend's breaker never opens and this is the ONLY thing that
        # would notice.
        broken = MagicMock()
        broken.set.return_value = None
        broken.get.return_value = None

        with patch("core.views.health_views.cache", broken):
            res = self.client.get(HEALTH_URL)

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["redis"], "degraded")

    def test_a_broken_database_is_a_503_naming_db(self):
        broken = MagicMock()
        broken.cursor.side_effect = Exception("could not connect to server")

        with patch("core.views.health_views.connection", broken):
            res = self.client.get(HEALTH_URL)

        self.assertEqual(res.status_code, 503)
        self.assertEqual(
            res.json(), {"status": "degraded", "db": "error", "redis": "ok"}
        )

    def test_one_failure_does_not_mask_the_other_component(self):
        """
        Both down must report BOTH down.

        A probe that short-circuits on the first failure tells you Redis is
        gone and leaves you guessing about the database — the two checks are
        deliberately independent.
        """
        broken_cache = MagicMock()
        broken_cache.set.side_effect = ConnectionError("redis is gone")
        broken_db = MagicMock()
        broken_db.cursor.side_effect = Exception("could not connect to server")

        with patch("core.views.health_views.cache", broken_cache), \
                patch("core.views.health_views.connection", broken_db):
            res = self.client.get(HEALTH_URL)

        # The DATABASE is what makes this a 503; the cache rides along in the
        # body so a reader is not left guessing about it.
        self.assertEqual(res.status_code, 503)
        self.assertEqual(
            res.json(),
            {"status": "degraded", "db": "error", "redis": "degraded"},
        )

    def test_no_exception_detail_reaches_the_body(self):
        # This URL is open to the internet. The connection string, the host and
        # the driver's error text stay in the log.
        broken = MagicMock()
        broken.cursor.side_effect = Exception(
            "FATAL: password authentication failed for user 'goatza'"
        )

        with patch("core.views.health_views.connection", broken):
            res = self.client.get(HEALTH_URL)

        self.assertEqual(
            sorted(res.json().keys()), ["db", "redis", "status"]
        )
        self.assertNotIn("password", res.content.decode())
