from django.contrib import admin

from matches.models import (
    MatchDiarySettings,
    MatchEntry,
    MatchEntryStat,
    SportMatchStatField,
)


@admin.register(SportMatchStatField)
class SportMatchStatFieldAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "sport",
        "short_label",
        "value_type",
        "unit",
        "is_primary",
        "order",
        "is_active",
    )

    list_filter = (
        "sport",
        "is_active",
        "is_primary",
    )

    search_fields = (
        "name",
        "short_label",
        "sport__name",
    )

    autocomplete_fields = (
        "sport",
    )

    # The catalog is edited here, and picking six positions out of a raw
    # multi-select is how you end up seeding the wrong ones.
    filter_horizontal = (
        "positions",
    )

    list_select_related = (
        "sport",
    )

    ordering = ("sport", "order")


class MatchEntryStatInline(admin.TabularInline):
    model = MatchEntryStat
    extra = 0
    autocomplete_fields = ("stat_field",)


@admin.register(MatchEntry)
class MatchEntryAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "sport",
        "date",
        "status",
        "result",
        "opponent_name",
        "is_deleted",
    )

    list_filter = (
        "status",
        "match_type",
        "sport",
        "is_deleted",
    )

    search_fields = (
        "opponent_name",
        "user__username",
        "user__email",
    )

    date_hierarchy = "date"

    autocomplete_fields = (
        "user",
        "sport",
        "position",
    )

    # CareerEntry admin exposes no search_fields.
    raw_id_fields = (
        "career_entry",
    )

    readonly_fields = (
        "created_at",
        "updated_at",
    )

    inlines = [MatchEntryStatInline]

    list_select_related = (
        "user",
        "sport",
    )

    ordering = ("-date", "-created_at")


@admin.register(MatchEntryStat)
class MatchEntryStatAdmin(admin.ModelAdmin):
    list_display = (
        "match_entry",
        "stat_field",
        "value",
    )

    list_filter = (
        "stat_field__sport",
        "stat_field",
    )

    search_fields = (
        "stat_field__name",
        "match_entry__opponent_name",
        "match_entry__user__username",
        "match_entry__user__email",
    )

    autocomplete_fields = (
        "stat_field",
    )

    # MatchEntry has search_fields, but a stat is reached through its match,
    # not picked out of thousands of them.
    raw_id_fields = (
        "match_entry",
    )

    list_select_related = (
        "match_entry",
        "stat_field",
    )


@admin.register(MatchDiarySettings)
class MatchDiarySettingsAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "showcase_summary",
        "current_streak_weeks",
        "longest_streak_weeks",
        "last_logged_at",
        "updated_at",
    )

    list_filter = (
        "showcase_summary",
    )

    search_fields = (
        "user__username",
        "user__email",
    )

    autocomplete_fields = (
        "user",
    )

    # Maintained by the service layer on every diary write — editing them by
    # hand only produces a number the next write overwrites.
    readonly_fields = (
        "current_streak_weeks",
        "longest_streak_weeks",
        "last_logged_at",
        "created_at",
        "updated_at",
    )

    list_select_related = (
        "user",
    )

    ordering = ("-updated_at",)
