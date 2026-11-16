"""
The admin changelist is the report, so each filter's result count is checked
against rows whose answer is known.
"""

import uuid
from datetime import timedelta

from django.conf import settings
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.pwa.models import AppInstall

CHANGELIST = "admin:pwa_appinstall_changelist"


# These render real admin pages. The Manifest storage in settings raises on any
# static file collectstatic has not written, which is every file on a fresh
# checkout or in CI. The plain storage resolves the same URLs without a manifest
# (the same override as organization/test_member_fixes.MemberAdminTests).
@override_settings(
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
    STORAGES={
        **settings.STORAGES,
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
        },
    },
)
class AppInstallAdminTests(TestCase):

    def setUp(self):
        self.admin = User.objects.create_superuser(
            email="admin@example.com", password="pass1234"
        )
        self.client.force_login(self.admin)

        self.player = User.objects.create_user(
            email="player@example.com", password="pass1234", username="pwaplayer"
        )

        now = timezone.now()

        # Installed and opened today, from our button.
        self.android = self._row(
            platform="android", browser="chrome", install_source="prompt",
            installed_at=now, first_opened_at=now, last_opened_at=now,
            open_days=3, user=self.player,
        )
        # An iPhone: never "installed", first opened 20 days ago.
        self.iphone = self._row(
            platform="ios", browser="safari",
            first_opened_at=now - timedelta(days=20),
            last_opened_at=now - timedelta(days=20), open_days=1,
        )
        # Installed from the browser menu, last opened two months ago.
        self.stale = self._row(
            platform="desktop", browser="edge", install_source="browser_menu",
            installed_at=now - timedelta(days=90),
            first_opened_at=now - timedelta(days=90),
            last_opened_at=now - timedelta(days=60), open_days=5,
        )
        # Neither timestamp: not an install.
        self.empty = self._row()

    def _row(self, **fields):
        return AppInstall.objects.create(device_id=uuid.uuid4(), **fields)

    def _changelist(self, **params):
        res = self.client.get(reverse(CHANGELIST), params)
        self.assertEqual(res.status_code, 200, params)
        return res.context["cl"]

    def _ids(self, cl):
        return {row.pk for row in cl.queryset}

    def test_the_changelist_loads_with_every_row(self):
        self.assertEqual(self._changelist().result_count, 4)

    def test_the_installed_filter(self):
        yes = self._changelist(installed="yes")
        self.assertEqual(yes.result_count, 3)
        self.assertEqual(self._ids(yes), {self.android.pk, self.iphone.pk, self.stale.pk})

        no = self._changelist(installed="no")
        self.assertEqual(self._ids(no), {self.empty.pk})

    def test_the_opened_recently_filter(self):
        self.assertEqual(self._ids(self._changelist(opened="7d")), {self.android.pk})
        self.assertEqual(
            self._ids(self._changelist(opened="30d")),
            {self.android.pk, self.iphone.pk},
        )

    def test_the_field_filters(self):
        self.assertEqual(self._ids(self._changelist(platform__exact="ios")), {self.iphone.pk})
        self.assertEqual(self._ids(self._changelist(browser__exact="edge")), {self.stale.pk})
        self.assertEqual(
            self._ids(self._changelist(install_source__exact="unknown")),
            {self.iphone.pk, self.empty.pk},
        )

    def test_filters_combine(self):
        cl = self._changelist(installed="yes", opened="30d", platform__exact="android")
        self.assertEqual(self._ids(cl), {self.android.pk})

    def test_the_date_hierarchy_and_search(self):
        year = timezone.now().year
        self.assertEqual(self._changelist(created_at__year=year).result_count, 4)

        self.assertEqual(self._ids(self._changelist(q="player@example.com")), {self.android.pk})
        self.assertEqual(self._ids(self._changelist(q="pwaplayer")), {self.android.pk})

    def test_no_add_view_only_and_delete_allowed(self):
        self.assertEqual(
            self.client.get(reverse("admin:pwa_appinstall_add")).status_code, 403
        )

        page = self.client.get(
            reverse("admin:pwa_appinstall_change", args=[self.android.pk])
        )
        self.assertEqual(page.status_code, 200)
        self.assertFalse(page.context["has_change_permission"])
        self.assertNotContains(page, 'name="_save"')

        res = self.client.post(
            reverse("admin:pwa_appinstall_delete", args=[self.empty.pk]),
            {"post": "yes"},
        )
        self.assertEqual(res.status_code, 302)
        self.assertFalse(AppInstall.objects.filter(pk=self.empty.pk).exists())
