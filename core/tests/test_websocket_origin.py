"""
The websocket Origin check (core.asgi).

A websocket handshake is NOT subject to the same-origin policy and CORS does
not apply to it, so without this validator any page on the internet could open
a socket to this server from a visitor's browser. These tests run against the
REAL ``core.asgi.application`` rather than a freshly built validator: the thing
that can regress is the wiring in asgi.py, not channels' matching logic.

``allowed_origins`` is patched on the live validator instead of being set with
``override_settings``, because the list is read once when asgi.py builds the
application at import time — an override_settings block would change a setting
nothing reads again and the test would silently assert nothing.

The load-bearing test is ``test_an_allowed_origin_still_connects``: a validator
that rejected EVERYTHING would pass the rejection tests and take the whole chat
feature down.
"""

from unittest.mock import patch

from asgiref.sync import async_to_sync
from channels.security.websocket import OriginValidator
from channels.testing import WebsocketCommunicator
from django.test import TransactionTestCase, override_settings
from rest_framework_simplejwt.tokens import AccessToken

from apps.accounts.models import User, UserProfile
from core.asgi import application
from apps.legal.testing import accept_current_terms
from apps.usernames.services.username_service import UsernameService

NOTIFICATIONS_PATH = "/ws/notifications/"

ALLOWED = "https://goatza.com"
EVIL = "https://goatza.com.evil.example"


@override_settings(
    # The real CHANNEL_LAYERS points at REDIS_URL, which a test run has no
    # reason to require. The consumer only needs a layer it can group_add on.
    CHANNEL_LAYERS={
        "default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}
    },
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class WebsocketOriginValidationTests(TransactionTestCase):
    """
    TransactionTestCase, not TestCase: the consumer reaches the database from
    another thread through ``database_sync_to_async``, which cannot see rows
    held open inside a TestCase's uncommitted transaction.
    """

    def setUp(self):
        self.user = User.objects.create_user(
            email="keeper@example.com",
            password="pass1234",
            username="keeper",
            role=User.Role.PLAYER,
        )
        accept_current_terms(self.user)
        UserProfile.objects.create(user=self.user, name="Keeper")
        UsernameService.claim("keeper", user=self.user)

        self.token = str(AccessToken.for_user(self.user))
        self.websocket_app = application.application_mapping["websocket"]

    # ---------------- helpers ----------------

    def _connect(self, origin, *, with_token=True):
        """
        Open a socket with `origin` and return (connected, code_or_subprotocol).

        The allow-list is forced to a single known origin for the duration of
        the call so the assertions do not depend on whatever CORS_ALLOWED_ORIGINS
        happens to hold in the environment running the suite.
        """
        headers = [] if origin is None else [(b"origin", origin.encode())]
        subprotocols = ["access_token", self.token] if with_token else []

        async def scenario():
            communicator = WebsocketCommunicator(
                application,
                NOTIFICATIONS_PATH,
                headers=headers,
                subprotocols=subprotocols,
            )
            result = await communicator.connect(timeout=5)
            await communicator.disconnect()
            return result

        with patch.object(self.websocket_app, "allowed_origins", [ALLOWED]):
            return async_to_sync(scenario)()

    # =================================================================
    # WIRING
    # =================================================================

    def test_the_websocket_router_is_wrapped_in_an_origin_validator(self):
        # Everything below is about behaviour; this is the one that fails
        # loudly if somebody unwraps asgi.py while refactoring.
        self.assertIsInstance(self.websocket_app, OriginValidator)

    # =================================================================
    # ACCEPTED
    # =================================================================

    def test_an_allowed_origin_still_connects(self):
        connected, subprotocol = self._connect(ALLOWED)

        self.assertTrue(connected)
        self.assertEqual(subprotocol, "access_token")

    # =================================================================
    # REJECTED
    # =================================================================

    def test_a_foreign_origin_is_rejected(self):
        connected, _ = self._connect("https://evil.example.com")

        self.assertFalse(connected)

    def test_a_lookalike_suffix_origin_is_rejected(self):
        # "goatza.com.evil.example" contains our domain as a prefix. A naive
        # startswith/endswith check would let it through; the validator
        # compares hostnames.
        connected, _ = self._connect(EVIL)

        self.assertFalse(connected)

    def test_a_valid_token_does_not_buy_a_bad_origin_a_connection(self):
        """
        The point of the validator.

        This caller holds a genuine access token for a real, active user — the
        rejection is about WHERE the handshake came from, and it happens before
        the token is even parsed.
        """
        connected, _ = self._connect(EVIL, with_token=True)

        self.assertFalse(connected)

    def test_a_missing_origin_header_is_rejected(self):
        # Documented consequence of the explicit allow-list: every non-browser
        # client (a native app, wscat) sends no Origin and is refused. Browsers
        # always send it and cannot be scripted to forge it, which is what makes
        # the check worth anything.
        connected, _ = self._connect(None)

        self.assertFalse(connected)
