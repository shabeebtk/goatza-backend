from django.contrib import admin
from .models import Follow


@admin.register(Follow)
class FollowAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "follower_display",
        "following_display",
        "created_at",
    )

    list_filter = (
        "created_at",
    )

    search_fields = (
        "follower_user__username",
        "follower_user__email",
        "following_user__username",
        "following_user__email",
        "follower_org__name",
        "following_org__name",
    )

    autocomplete_fields = (
        "follower_user",
        "following_user",
        "follower_org",
        "following_org",
    )

    readonly_fields = ("created_at",)

    ordering = ("-created_at",)

    list_select_related = (
        "follower_user",
        "following_user",
        "follower_org",
        "following_org",
    )

    # Better display (human readable)
    def follower_display(self, obj):
        if obj.follower_user:
            return f"User: {obj.follower_user.username or obj.follower_user.id}"
        return f"Org: {obj.follower_org.name if obj.follower_org else obj.follower_org_id}"

    follower_display.short_description = "Follower"

    def following_display(self, obj):
        if obj.following_user:
            return f"User: {obj.following_user.username or obj.following_user.id}"
        return f"Org: {obj.following_org.name if obj.following_org else obj.following_org_id}"

    following_display.short_description = "Following"