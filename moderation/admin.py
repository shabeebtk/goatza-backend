from django.contrib import admin
from .models import Block


@admin.register(Block)
class BlockAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "blocker_display",
        "blocked_display",
        "created_at",
    )

    list_filter = (
        "created_at",
    )

    search_fields = (
        "blocker_user__username",
        "blocker_user__email",
        "blocked_user__username",
        "blocked_user__email",
        "blocker_org__name",
        "blocked_org__name",
    )

    autocomplete_fields = (
        "blocker_user",
        "blocked_user",
        "blocker_org",
        "blocked_org",
    )

    readonly_fields = ("created_at",)

    ordering = ("-created_at",)

    list_select_related = (
        "blocker_user",
        "blocked_user",
        "blocker_org",
        "blocked_org",
    )

    # Better display (human readable)
    def blocker_display(self, obj):
        if obj.blocker_user:
            return f"User: {obj.blocker_user.username or obj.blocker_user.id}"
        return f"Org: {obj.blocker_org.name if obj.blocker_org else obj.blocker_org_id}"

    blocker_display.short_description = "Blocker"

    def blocked_display(self, obj):
        if obj.blocked_user:
            return f"User: {obj.blocked_user.username or obj.blocked_user.id}"
        return f"Org: {obj.blocked_org.name if obj.blocked_org else obj.blocked_org_id}"

    blocked_display.short_description = "Blocked"
