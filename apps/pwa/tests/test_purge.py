"""
Account purge detaches the person from their app installs.

The purge keeps an emptied User row, so on_delete=SET_NULL never fires; the
purge has to clear the link itself. The install row stays — it is a count, and
it no longer names anyone.
"""

import os
import uuid
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.accounts.management.commands.purge_deleted_accounts import ANON_EMAIL_DOMAIN
from apps.accounts.models import User, UserProfile
from apps.pwa.models import AppInstall
from apps.usernames.services.username_service import UsernameService


@override_settings(
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class PurgeDetachesAppInstallsTests(TestCase):

    def setUp(self):
        self.leaver = self._user("leaver")
        self.stayer = self._user("stayer")

        now = timezone.now()
        self.leaver_install = AppInstall.objects.create(
            device_id=uuid.uuid4(), user=self.leaver,
            installed_at=now, first_opened_at=now, last_opened_at=now, open_days=4,
        )
        self.stayer_install = AppInstall.objects.create(
            device_id=uuid.uuid4(), user=self.stayer, installed_at=now,
        )

        # What confirm_account_deletion leaves behind, past the window.
        User.objects.filter(pk=self.leaver.pk).update(
            is_active=False, deletion_requested_at=now - timedelta(days=60)
        )

    def _user(self, username):
        user = User.objects.create_user(
            email=f"{username}@example.com", password="pass1234"
        )
        UserProfile.objects.create(user=user, name=username.title())
        UsernameService.claim(username, user=user)
        return user

    def _purge(self):
        with patch.dict(os.environ, {"ACCOUNT_PURGE_AFTER_DAYS": "30"}):
            call_command("purge_deleted_accounts", stdout=StringIO())

    def test_the_purge_clears_user_and_keeps_the_install(self):
        self._purge()

        self.leaver.refresh_from_db()
        self.assertTrue(self.leaver.email.endswith(f"@{ANON_EMAIL_DOMAIN}"))

        self.leaver_install.refresh_from_db()
        self.assertIsNone(self.leaver_install.user_id)
        self.assertEqual(self.leaver_install.open_days, 4)
        self.assertTrue(self.leaver_install.is_installed)

    def test_someone_elses_install_is_untouched(self):
        self._purge()

        self.stayer_install.refresh_from_db()
        self.assertEqual(self.stayer_install.user_id, self.stayer.pk)
