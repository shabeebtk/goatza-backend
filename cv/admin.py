from django.contrib import admin

from cv.models import PlayerCVSettings


@admin.register(PlayerCVSettings)
class PlayerCVSettingsAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "user",
        "is_enabled",
        "show_contact",
        "show_attributes",
        "show_career",
        "show_achievements",
        "show_highlights",
        "views_count",
        "updated_at",
    )

    list_filter = (
        "is_enabled",
        "show_contact",
        "created_at",
    )

    search_fields = (
        "user__username",
        "user__email",
    )

    autocomplete_fields = (
        "user",
    )

    # Maintained by CVService.record_view — never edited by hand, or the
    # counter stops meaning anything.
    readonly_fields = (
        "views_count",
        "created_at",
        "updated_at",
    )

    list_select_related = (
        "user",
    )

    ordering = ("-created_at",)
