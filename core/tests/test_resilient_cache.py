"""
core.cache.resilient — Redis is an optimisation, not a dependency.

The property under test is the one the app was broken by: a cache call that
cannot reach Redis must answer like a MISS, not raise into the view. Postgres
is the system of record; losing Redis must make the app slower, never dead.

Two of these are load-bearing and the rest support them:

``test_token_refresh_succeeds_with_the_cache_failing`` is the bug that started
this. ``TokenRefreshAPIView`` writes a 60-second replay entry on every call, so
an unreachable Redis made ``POST /user/token/refresh`` answer 500 and took the
whole app down with it.

``test_otp_send_returns_503_and_sends_no_email`` guards the one deliberate
exception: an OTP lives ONLY in the cache, so a silent no-op there would mail a
code that can never verify and tell the user "invalid or expired". That path
fails loudly on purpose.

The unit tests patch the backend's client rather than blackholing a port: what
is under test is how we react to a RedisError, not that redis-py raises one.
"""

from unittest.mock import patch

from django.test import TestCase, override_settings
from redis.exceptions import ConnectionError as RedisConnectionError
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from apps.accounts.models import User, UserProfile
from apps.legal.testing import accept_current_terms
from core.cache import resilient
from core.cache.resilient import ResilientRedisCache
from utils.otp_validation import OTP_UNAVAILABLE_MESSAGE

# Nothing in this file dials this: the unit tests patch the client, and the
# request tests rely on the 1s connect timeout the real settings configure.
DEAD_REDIS = "redis://127.0.0.1:6399/0"

# The resilient backend pointed at a Redis that is not there, with the same
# short timeouts core.settings uses so a dead Redis costs one second once
# rather than hanging.
DEAD_CACHE = {
    "default": {
        "BACKEND": "core.cache.resilient.ResilientRedisCache",
        "LOCATION": DEAD_REDIS,
        "OPTIONS": {"socket_connect_timeout": 1, "socket_timeout": 1},
    }
}


class _ExplodingClient:
    """Stands in for RedisCacheClient: every operation raises, and counts
    the attempt so a test can prove no socket was tried."""

    def __init__(self):
        self.calls = 0

    def _boom(self, *args, **kwargs):
        self.calls += 1
        raise RedisConnectionError("Error 10061 connecting to 127.0.0.1:6379")

    get = set = add = delete = touch = incr = _boom
    get_many = set_many = delete_many = has_key = clear = _boom


class ResilientCacheBackendTests(TestCase):

    def setUp(self):
        resilient.reset_cache_state()
        self.addCleanup(resilient.reset_cache_state)
        self.backend = ResilientRedisCache(DEAD_REDIS, {})

    def test_get_returns_the_default_instead_of_raising(self):
        with patch.object(ResilientRedisCache, "_cache", _ExplodingClient()):
            # The caller's OWN default, so a degraded read is indistinguishable
            # from a miss and no caller needs a new branch for it.
            self.assertIsNone(self.backend.get("some:key"))
            self.assertEqual(self.backend.get("some:key", "fallback"), "fallback")

        self.assertTrue(resilient.is_degraded())

    def test_add_returns_false_instead_of_raising(self):
        """
        ``add`` is a "count this once" latch — the CV view counter and the
        applicant-alert claim. False means somebody else holds it, so the thing
        is NOT counted. Under-counting is the right failure: double-counting
        corrupts a number the org reads, and a 500 loses the whole request.
        """
        with patch.object(ResilientRedisCache, "_cache", _ExplodingClient()):
            self.assertIs(self.backend.add("latch:key", 1, 30), False)

    @override_settings(CACHE_FAILURE_COOLDOWN=30)
    def test_the_cooldown_stops_the_second_call_dialling_redis(self):
        """
        Catching the error alone would leave every request paying the TCP
        connect timeout, which turns a fast app into an unusably slow one —
        worse than the 500, not better. Only the first call in a window may
        touch the client at all.
        """
        client = _ExplodingClient()

        with patch.object(ResilientRedisCache, "_cache", client):
            self.backend.get("a")
            self.assertEqual(client.calls, 1)

            # Everything from here is answered without a socket.
            for _ in range(20):
                self.backend.get("a")
                self.backend.set("a", 1)
                self.backend.add("a", 1, 30)
                self.backend.incr("a")

            self.assertEqual(client.calls, 1)


@override_settings(CACHES=DEAD_CACHE)
class DeadCacheRequestTests(TestCase):
    """
    The resilient backend wired in as the real one, so these take the path a
    request actually takes — through DRF, the throttles and the view.
    """

    def setUp(self):
        resilient.reset_cache_state()
        self.addCleanup(resilient.reset_cache_state)
        self.client = APIClient()

        self.user = User.objects.create_user(
            email="deadcache@example.com",
            username="deadcacheuser",
            password="password123",
            role=User.Role.PLAYER,
        )
        accept_current_terms(self.user)
        UserProfile.objects.create(user=self.user, name="Dead Cache")

    def test_token_refresh_succeeds_with_the_cache_failing(self):
        """
        THE ORIGINAL BUG. The rotation still happens and the caller still gets
        a new pair; the only thing lost is the 60-second replay grace, which is
        exactly the right thing to lose.
        """
        refresh = RefreshToken.for_user(self.user)
        self.client.cookies["refresh_token"] = str(refresh)

        res = self.client.post("/user/token/refresh", {}, format="json")

        self.assertEqual(res.status_code, 200, res.data)
        self.assertTrue(res.data["success"])
        self.assertTrue(res.data["data"]["access_token"])
        # And the outage was noticed rather than swallowed.
        self.assertTrue(resilient.is_degraded())

    @patch("apps.accounts.views.user_auth_views.send_password_reset_otp_email")
    def test_otp_send_returns_503_and_sends_no_email(self, send_mail):
        """
        The one path where a silent no-op is worse than an error: the code lives
        only in the cache, so a dropped write means the user is told "invalid or
        expired" for a code they typed correctly.

        503 (not 500), an honest message naming no infrastructure, and — the
        part that matters most — NO EMAIL, because a code that can never verify
        is the worst outcome available.
        """
        res = self.client.post(
            "/user/forgot/password",
            {"email": self.user.email},
            format="json",
        )

        self.assertEqual(res.status_code, 503, res.data)
        self.assertFalse(res.data["success"])
        self.assertEqual(res.data["message"], OTP_UNAVAILABLE_MESSAGE)
        send_mail.assert_not_called()

        # No infrastructure detail reaches the user.
        body = res.content.decode().lower()
        for leak in ("redis", "cache", "connection"):
            self.assertNotIn(leak, body)
