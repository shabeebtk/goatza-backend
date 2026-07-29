from django.contrib import admin
from highlights.models import Highlight


@admin.register(Highlight)
class HighlightAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "user",
        "title",
        "visibility",
        "order",
        "duration",
        "views_count",
        "is_deleted",
        "created_at",
    )

    list_filter = (
        "visibility",
        "is_deleted",
        "created_at",
    )

    search_fields = (
        "title",
        "public_id",
        "user__username",
        "user__email",
    )

    autocomplete_fields = (
        "user",
    )

    # Post has no admin search_fields, so autocomplete is not available for it
    raw_id_fields = (
        "source_post",
    )

    readonly_fields = (
        "views_count",
        "created_at",
        "updated_at",
    )

    list_select_related = (
        "user",
    )

    ordering = ("-created_at",)
