"""
App installs — the only place these numbers are read.

The changelist's result count IS the report: filter to "Installed: yes" for
installs, add "Opened recently: last 7 days" for weekly actives, add a
platform for the split. Nothing here is editable; the rows are written by
POST /app/events alone.
"""

from datetime import timedelta

from django.contrib import admin
from django.db.models import Q
from django.utils import timezone

from .models import AppInstall


class InstalledFilter(admin.SimpleListFilter):
    """
    Installed = ``installed_at`` OR ``first_opened_at`` set — an iPhone never
    reports "installed", so its first open from the home screen is the install.
    Mirrors ``AppInstall.is_installed``.
    """

    title = "Installed"
    parameter_name = "installed"

    def lookups(self, request, model_admin):
        return (("yes", "Yes"), ("no", "No"))

    def queryset(self, request, queryset):
        installed = Q(installed_at__isnull=False) | Q(first_opened_at__isnull=False)
        if self.value() == "yes":
            return queryset.filter(installed)
        if self.value() == "no":
            return queryset.exclude(installed)
        return queryset


class OpenedRecentlyFilter(admin.SimpleListFilter):
    """By ``last_opened_at``: active in the last 7 or 30 days."""

    title = "Opened recently"
    parameter_name = "opened"

    WINDOWS = {"7d": 7, "30d": 30}

    def lookups(self, request, model_admin):
        return (("7d", "Last 7 days"), ("30d", "Last 30 days"))

    def queryset(self, request, queryset):
        days = self.WINDOWS.get(self.value())
        if days is None:
            return queryset
        return queryset.filter(
            last_opened_at__gte=timezone.now() - timedelta(days=days)
        )


@admin.register(AppInstall)
class AppInstallAdmin(admin.ModelAdmin):

    list_display = (
        "platform",
        "browser",
        "install_source",
        "user",
        "installed_at",
        "first_opened_at",
        "last_opened_at",
        "open_days",
    )

    list_filter = (
        "platform",
        "browser",
        "install_source",
        InstalledFilter,
        OpenedRecentlyFilter,
    )

    date_hierarchy = "created_at"
    search_fields = ("user__email", "user__username")
    raw_id_fields = ("user",)
    list_select_related = ("user",)
    ordering = ("-created_at",)

    readonly_fields = (
        "id",
        "device_id",
        "platform",
        "browser",
        "install_source",
        "user",
        "installed_at",
        "first_opened_at",
        "last_opened_at",
        "open_days",
        "is_installed",
        "created_at",
        "updated_at",
    )

    # Rows come from the client; a hand-made one would only inflate the count.
    def has_add_permission(self, request):
        return False

    # View-only, so there is no Save button: every field is read-only anyway,
    # and a Save would still bump updated_at. Deleting stays allowed.
    def has_change_permission(self, request, obj=None):
        return False

    @admin.display(boolean=True, description="Installed")
    def is_installed(self, obj):
        return obj.is_installed
