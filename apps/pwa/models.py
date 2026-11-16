"""
App installs: how many people put Goatza on their home screen, and how often
they open it from there.

One row per DEVICE, not per event. ``device_id`` is a random UUID the browser
makes and keeps in its own storage — it is not a fingerprint and nothing about
the device is derived from it. Clearing site data makes a new one, so a person
who reinstalls is counted twice. Accepted: the number is a trend, not a ledger.

WHAT IS NEVER STORED: an IP address or a user-agent string. ``platform`` and
``browser`` are coarse buckets the client picks from a fixed list, and an
unknown value lands in ``other`` rather than being kept verbatim.

Nobody reads these numbers in the app. The Django admin changelist is the
report: its result count, with a filter applied, IS the number.
"""

from django.conf import settings
from django.db import models

from shared.models import BaseUUIDModel


class Platform(models.TextChoices):
    ANDROID = "android", "Android"
    IOS = "ios", "iOS"
    DESKTOP = "desktop", "Desktop"
    OTHER = "other", "Other"


class Browser(models.TextChoices):
    CHROME = "chrome", "Chrome"
    SAFARI = "safari", "Safari"
    SAMSUNG = "samsung", "Samsung Internet"
    FIREFOX = "firefox", "Firefox"
    EDGE = "edge", "Edge"
    OPERA = "opera", "Opera"
    OTHER = "other", "Other"


class InstallSource(models.TextChoices):
    PROMPT = "prompt", "Our Install button"
    BROWSER_MENU = "browser_menu", "Browser menu"
    UNKNOWN = "unknown", "Unknown"


class AppEvent(models.TextChoices):
    """What the client reports. Not stored as a column — each one moves fields."""

    INSTALLED = "installed", "Installed"
    OPENED = "opened", "Opened"


class AppInstall(BaseUUIDModel):
    """One browser's installed copy of the app."""

    device_id = models.UUIDField(unique=True)

    platform = models.CharField(
        max_length=10, choices=Platform.choices, default=Platform.OTHER
    )
    browser = models.CharField(
        max_length=10, choices=Browser.choices, default=Browser.OTHER
    )

    # Written by the first "installed" that knows, never overwritten after:
    # how the app got installed does not change because a later event says
    # something different.
    install_source = models.CharField(
        max_length=15,
        choices=InstallSource.choices,
        default=InstallSource.UNKNOWN,
    )

    # The person last signed in on this device. SET_NULL because the install
    # outlives the account; the purge clears it explicitly as well, since a
    # purged account keeps its (emptied) User row and SET_NULL never fires.
    # Never set for a minor whose guardian consent is pending or withdrawn.
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="app_installs",
    )

    # An iPhone never reports "installed" (Safari has no install event), so
    # its first open from the home screen IS the install — see is_installed.
    installed_at = models.DateTimeField(null=True, blank=True)
    first_opened_at = models.DateTimeField(null=True, blank=True)
    last_opened_at = models.DateTimeField(null=True, blank=True)

    # How many different days (Asia/Kolkata calendar) the installed app was
    # opened. A retention signal that needs no event table.
    open_days = models.PositiveIntegerField(default=0)

    # BaseUUIDModel carries only the id, so the timestamps live here.
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "app_installs"
        ordering = ["-created_at"]
        verbose_name = "App install"
        verbose_name_plural = "App installs"

    def __str__(self):
        return f"{self.get_platform_display()} · {self.get_browser_display()}"

    @property
    def is_installed(self):
        return self.installed_at is not None or self.first_opened_at is not None
