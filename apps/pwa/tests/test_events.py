"""
POST /app/events, from the outside.

``cache.clear()`` in every setUp: the endpoint is throttled per IP, every test
client is 127.0.0.1, and DRF keeps the counter in the shared cache.
"""

import datetime
import uuid
from unittest.mock import patch

from django.conf import settings
from django.core.cache import cache
from django.test import override_settings
from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.pwa.models import AppInstall
from apps.pwa.throttles import AppEventsThrottle
from apps.pwa.views.app_event_views import AppEventsAPIView

URL = "/app/events"

# The service reads django.utils.timezone.now; patching it there moves every
# clock the row sees, auto_now included.
NOW = "django.utils.timezone.now"

UTC = datetime.timezone.utc


def utc(*args):
    return datetime.datetime(*args, tzinfo=UTC)


@override_settings(
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class AppEventsTestBase(APITestCase):

    def setUp(self):
        cache.clear()
        self.device = str(uuid.uuid4())

    def _send(self, event, device=None, **fields):
        body = {
            "device_id": device or self.device,
            "event": event,
            "platform": "android",
            "browser": "chrome",
        }
        body.update(fields)
        return self.client.post(URL, body, format="json")

    def _send_at(self, moment, event, **fields):
        with patch(NOW, return_value=moment):
            return self._send(event, **fields)

    def _row(self):
        return AppInstall.objects.get(device_id=self.device)

    def _user(self, email, consent=User.GuardianConsentStatus.NOT_NEEDED):
        user = User.objects.create_user(email=email, password="pass1234")
        User.objects.filter(pk=user.pk).update(guardian_consent_status=consent)
        user.refresh_from_db()
        return user


class EventTests(AppEventsTestBase):

    def test_installed_then_opened_land_on_the_same_row(self):
        res = self._send("installed", source="prompt")

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["data"], {"ok": True})

        self.assertEqual(self._send("opened").status_code, 200)

        self.assertEqual(AppInstall.objects.count(), 1)
        row = self._row()
        self.assertIsNotNone(row.installed_at)
        self.assertIsNotNone(row.first_opened_at)
        self.assertEqual(row.last_opened_at, row.first_opened_at)
        self.assertEqual(row.open_days, 1)
        self.assertEqual(row.install_source, "prompt")
        self.assertEqual(row.platform, "android")
        self.assertEqual(row.browser, "chrome")
        self.assertTrue(row.is_installed)

    def test_installed_twice_keeps_the_first_time_and_source(self):
        first = utc(2026, 11, 10, 9, 0)
        self._send_at(first, "installed", source="prompt")
        self._send_at(utc(2026, 11, 12, 9, 0), "installed", source="browser_menu")

        row = self._row()
        self.assertEqual(row.installed_at, first)
        self.assertEqual(row.install_source, "prompt")

    def test_an_unknown_source_is_filled_in_by_a_later_install(self):
        self._send("installed")
        self.assertEqual(self._row().install_source, "unknown")

        self._send("installed", source="browser_menu")
        self.assertEqual(self._row().install_source, "browser_menu")

    def test_an_iphone_first_open_is_the_install(self):
        # Safari never sends "installed".
        self._send("opened", platform="ios", browser="safari")

        row = self._row()
        self.assertIsNone(row.installed_at)
        self.assertIsNotNone(row.first_opened_at)
        self.assertTrue(row.is_installed)


class OpenDaysTests(AppEventsTestBase):
    """open_days counts Asia/Kolkata calendar days (UTC+5:30), not UTC ones."""

    def test_open_days_goes_up_once_per_india_day(self):
        # 18:00 UTC on the 14th is 23:30 IST on the 14th.
        self._send_at(utc(2026, 11, 14, 18, 0), "opened")
        self.assertEqual(self._row().open_days, 1)

        # 18:20 UTC = 23:50 IST, still the 14th: same day.
        self._send_at(utc(2026, 11, 14, 18, 20), "opened")
        self.assertEqual(self._row().open_days, 1)

        # 18:40 UTC = 00:10 IST on the 15th, the same UTC day: a new day.
        self._send_at(utc(2026, 11, 14, 18, 40), "opened")
        self.assertEqual(self._row().open_days, 2)

        # 00:10 UTC on the 15th = 05:40 IST on the 15th. A new UTC day, the
        # same India day: no change.
        self._send_at(utc(2026, 11, 15, 0, 10), "opened")

        row = self._row()
        self.assertEqual(row.open_days, 2)
        self.assertEqual(row.first_opened_at, utc(2026, 11, 14, 18, 0))
        self.assertEqual(row.last_opened_at, utc(2026, 11, 15, 0, 10))

    def test_an_install_alone_is_not_an_open_day(self):
        self._send("installed")

        row = self._row()
        self.assertEqual(row.open_days, 0)
        self.assertIsNone(row.last_opened_at)


class ValidationTests(AppEventsTestBase):

    def _assert_invalid(self, res):
        self.assertEqual(res.status_code, 400)
        body = res.json()
        self.assertFalse(body["success"])
        self.assertEqual(body["data"], {"code": "invalid"})

    def test_a_bad_device_id_is_400(self):
        for bad in ("not-a-uuid", "", 12345, None, ["x"]):
            with self.subTest(device_id=bad):
                self._assert_invalid(self.client.post(
                    URL, {"device_id": bad, "event": "opened"}, format="json"
                ))

        self.assertFalse(AppInstall.objects.exists())

    def test_a_bad_event_is_400(self):
        for bad in ("launched", "", None, 1):
            self._assert_invalid(self._send(bad))

        self.assertFalse(AppInstall.objects.exists())

    def test_a_missing_body_or_broken_json_is_400(self):
        self._assert_invalid(self.client.post(URL, {}, format="json"))
        self._assert_invalid(
            self.client.post(URL, "{not json", content_type="application/json")
        )
        self._assert_invalid(self.client.post(URL, [1, 2], format="json"))

    def test_an_unknown_platform_or_browser_is_stored_as_other(self):
        self._send("opened", platform="playstation", browser="netscape")

        row = self._row()
        self.assertEqual(row.platform, "other")
        self.assertEqual(row.browser, "other")

    def test_an_unknown_or_missing_source_is_unknown(self):
        self._send("installed", source="carrier_preload")
        self.assertEqual(self._row().install_source, "unknown")

    def test_other_never_replaces_a_known_platform(self):
        self._send("opened", platform="android", browser="samsung")
        self._send("opened", platform="other", browser="nonsense")

        row = self._row()
        self.assertEqual(row.platform, "android")
        self.assertEqual(row.browser, "samsung")

    def test_the_newest_known_platform_wins(self):
        self._send("opened", platform="android", browser="chrome")
        self._send("opened", platform="desktop", browser="edge")

        row = self._row()
        self.assertEqual(row.platform, "desktop")
        self.assertEqual(row.browser, "edge")


class UserLinkTests(AppEventsTestBase):

    def test_an_anonymous_event_names_nobody(self):
        self._send("installed")
        self.assertIsNone(self._row().user_id)

    def test_a_signed_in_user_is_attached(self):
        user = self._user("adult@example.com")
        self.client.force_authenticate(user=user)

        self._send("opened")

        self.assertEqual(self._row().user_id, user.pk)

    def test_an_approved_minor_is_attached(self):
        user = self._user("approved@example.com", User.GuardianConsentStatus.APPROVED)
        self.client.force_authenticate(user=user)

        self._send("opened")

        self.assertEqual(self._row().user_id, user.pk)

    def test_a_minor_without_consent_is_counted_but_not_attached(self):
        for status in (
            User.GuardianConsentStatus.PENDING,
            User.GuardianConsentStatus.WITHDRAWN,
        ):
            with self.subTest(status=status):
                device = str(uuid.uuid4())
                user = self._user(f"{status}@example.com", status)
                self.client.force_authenticate(user=user)

                res = self._send("opened", device=device)

                self.assertEqual(res.status_code, 200)
                row = AppInstall.objects.get(device_id=device)
                self.assertIsNone(row.user_id)
                self.assertEqual(row.open_days, 1)

    def test_the_last_signed_in_person_replaces_the_previous_one(self):
        first = self._user("first@example.com")
        second = self._user("second@example.com")

        self.client.force_authenticate(user=first)
        self._send("opened")
        self.client.force_authenticate(user=second)
        self._send("opened")

        self.assertEqual(self._row().user_id, second.pk)


class ThrottleTests(AppEventsTestBase):

    def test_the_scope_is_configured(self):
        self.assertEqual(AppEventsThrottle.scope, "app_events")
        self.assertEqual(
            settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]["app_events"],
            "60/hour",
        )
        self.assertEqual(AppEventsAPIView.throttle_classes, [AppEventsThrottle])

    def test_the_61st_event_in_an_hour_is_refused_in_the_envelope(self):
        for _ in range(60):
            self.assertEqual(self._send("opened").status_code, 200)

        res = self._send("opened")

        self.assertEqual(res.status_code, 429)
        body = res.json()
        self.assertNotIn("detail", body)
        self.assertFalse(body["success"])
        self.assertEqual(body["data"]["code"], "throttled")
        self.assertGreater(body["data"]["retry_after"], 0)

    def test_signing_in_does_not_buy_a_fresh_bucket(self):
        # A plain AnonRateThrottle skips a signed-in caller; this one keys on
        # the IP for everyone.
        for _ in range(60):
            self._send("opened")

        self.client.force_authenticate(user=self._user("late@example.com"))

        self.assertEqual(self._send("opened").status_code, 429)
