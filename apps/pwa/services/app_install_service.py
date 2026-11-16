"""
Recording one install/open event against its device's row.

Every event for a device lands on the same row, under a row lock, so two
events sent together (an "installed" and the first "opened" fire within
milliseconds of each other on Android) cannot lose each other's fields.
"""

from zoneinfo import ZoneInfo

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.pwa.models import AppEvent, AppInstall, Browser, InstallSource, Platform

# Whose calendar decides "a different day" for open_days. Fixed, not the
# server's TIME_ZONE (UTC): an open at 01:00 IST must not count as the same day
# as one at 23:00 IST the evening before, which is what UTC would say.
OPEN_DAY_TIMEZONE = ZoneInfo("Asia/Kolkata")

# A child whose parent has not said yes (or has taken it back) is never linked
# to a device row: tying their usage to their account is processing their data,
# which is the thing the guardian gate exists to stop. The install still counts.
UNLINKABLE_CONSENT = frozenset({
    User.GuardianConsentStatus.PENDING,
    User.GuardianConsentStatus.WITHDRAWN,
})


def _open_day(moment):
    return timezone.localtime(moment, OPEN_DAY_TIMEZONE).date()


def _linkable_user(user):
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    if user.guardian_consent_status in UNLINKABLE_CONSENT:
        return None
    return user


def _locked_row(device_id):
    """
    The device's row, created if needed, locked for this transaction.

    The common case — a device we already know — is one locked read. A first
    event inserts in a savepoint; if a concurrent first event for the same
    device got there first, the unique index refuses ours, and the winner's row
    is committed by the time the IntegrityError reaches us, so we read it again.
    """
    row = AppInstall.objects.select_for_update().filter(device_id=device_id).first()
    if row is not None:
        return row

    try:
        with transaction.atomic():
            AppInstall.objects.create(device_id=device_id)
    except IntegrityError:
        pass

    return AppInstall.objects.select_for_update().get(device_id=device_id)


class AppInstallService:

    @staticmethod
    def record(event, *, user=None):
        """
        Apply one ``ParsedAppEvent``. Returns the saved row.

        installed — ``installed_at`` the first time, ``install_source`` while
        it is still unknown. Neither is ever overwritten afterwards.

        opened — ``first_opened_at`` the first time; ``open_days`` goes up when
        this is the first open on its Asia/Kolkata calendar day; then
        ``last_opened_at`` is now.

        Either — platform and browser follow the newest event unless it says
        ``other`` (a known value is never replaced by not knowing); ``user``
        becomes the signed-in caller unless they are a minor without consent.
        """
        now = timezone.now()
        changed = set()

        with transaction.atomic():
            install = _locked_row(event.device_id)

            if event.event == AppEvent.INSTALLED:
                if install.installed_at is None:
                    install.installed_at = now
                    changed.add("installed_at")
                if (
                    install.install_source == InstallSource.UNKNOWN
                    and event.source != InstallSource.UNKNOWN
                ):
                    install.install_source = event.source
                    changed.add("install_source")

            elif event.event == AppEvent.OPENED:
                if install.first_opened_at is None:
                    install.first_opened_at = now
                    changed.add("first_opened_at")
                if (
                    install.last_opened_at is None
                    or _open_day(install.last_opened_at) < _open_day(now)
                ):
                    install.open_days += 1
                    changed.add("open_days")
                install.last_opened_at = now
                changed.add("last_opened_at")

            if event.platform != Platform.OTHER and install.platform != event.platform:
                install.platform = event.platform
                changed.add("platform")

            if event.browser != Browser.OTHER and install.browser != event.browser:
                install.browser = event.browser
                changed.add("browser")

            linkable = _linkable_user(user)
            if linkable is not None and install.user_id != linkable.pk:
                install.user = linkable
                changed.add("user")

            if changed:
                install.save(update_fields=sorted(changed | {"updated_at"}))

        return install
