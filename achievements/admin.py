from django.contrib import admin
from achievements.models import Achievement


@admin.register(Achievement)
class AchievementAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "title",
        "achievement_type",
        "sport",
        "awarded_by_name",
        "level",
        "achieved_date",
        "verification_status",
        "is_pinned",
    )

    list_filter = (
        "verification_status",
        "achievement_type",
        "level",
        "is_pinned",
    )

    search_fields = (
        "title",
        "event_name",
        "awarded_by_name",
        "user__username",
        "user__email",
        "awarded_by__name",
    )

    autocomplete_fields = (
        "user",
        "awarded_by",
        "sport",
        "verified_by",
    )

    # CareerEntry admin exposes search_fields, but the stint an award belongs to
    # is picked by its owner, not from here.
    raw_id_fields = (
        "career_entry",
    )

    readonly_fields = (
        "created_at",
        "updated_at",
    )

    list_select_related = (
        "user",
        "sport",
    )

    ordering = ("-is_pinned", "-achieved_date", "-created_at")
