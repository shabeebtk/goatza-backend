from django.contrib import admin
from highlights.models import Highlight, HighlightView


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


@admin.register(HighlightView)
class HighlightViewAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "highlight",
        "viewer_display",
        "is_recruiter",
        "viewed_on",
        "created_at",
    )

    list_filter = (
        "is_recruiter",
        "viewed_on",
    )

    search_fields = (
        "highlight__title",
        "viewer_user__username",
        "viewer_user__email",
        "viewer_org__name",
    )

    autocomplete_fields = (
        "viewer_user",
    )

    # Neither Highlight nor Organization exposes admin search_fields.
    raw_id_fields = (
        "highlight",
        "viewer_org",
    )

    readonly_fields = ("created_at",)

    list_select_related = (
        "highlight",
        "viewer_user",
        "viewer_org",
    )

    ordering = ("-created_at",)

    def viewer_display(self, obj):
        if obj.viewer_user:
            return f"User: {obj.viewer_user.username or obj.viewer_user_id}"
        return f"Org: {obj.viewer_org.name if obj.viewer_org else obj.viewer_org_id}"

    viewer_display.short_description = "Viewer"
