from django.contrib import admin

from apps.squads.models import Squad, SquadMember, SquadStaff, SportSquadConfig


@admin.register(SportSquadConfig)
class SportSquadConfigAdmin(admin.ModelAdmin):
    """Editable: seeded catalog data. A re-run of seed_squad_configs resets the fields it seeds."""

    list_display = (
        "sport",
        "uses_positions",
        "uses_jersey_numbers",
        "member_noun",
        "member_noun_plural",
        "typical_squad_size",
    )

    search_fields = ("sport__name",)

    autocomplete_fields = ("sport",)

    list_select_related = ("sport",)

    ordering = ("sport__name",)


class SquadStaffInline(admin.TabularInline):
    """
    Role, title and order are editable; the person is not. Picking a member
    means checking they belong to the squad's organization, which the app does.
    """

    model = SquadStaff
    extra = 0
    fields = ("member", "role", "title", "order", "created_at")
    readonly_fields = ("member", "created_at")

    def has_add_permission(self, request, obj=None):
        return False

    def get_queryset(self, request):
        return super().get_queryset(request).select_related(
            "member__user", "member__organization"
        )


@admin.register(Squad)
class SquadAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "organization",
        "sport",
        "members_count",
        "pending_count",
        "is_public",
        "invite_enabled",
        "is_deleted",
        "created_at",
    )

    list_filter = (
        "sport",
        "is_public",
        "is_deleted",
    )

    search_fields = (
        "name",
        "organization__name",
    )

    autocomplete_fields = ("organization", "sport")

    # The counters are kept by the services; the token is reset in the app
    # (which is what revokes the old link); the creator is history.
    readonly_fields = (
        "members_count",
        "pending_count",
        "invite_token",
        "created_by_member",
        "deleted_at",
        "created_at",
        "updated_at",
    )

    list_select_related = ("organization", "sport")

    ordering = ("-created_at",)

    inlines = [SquadStaffInline]


@admin.register(SquadMember)
class SquadMemberAdmin(admin.ModelAdmin):
    """
    Read-only except ``notes``. Every status change goes through the app, which
    keeps the squad's counters and the player's career entry right. No add and
    no delete for the same reason — and because the admin checks related rows
    before a cascade, a squad with membership rows cannot be hard-deleted here
    either, which keeps the squad's history.
    """

    list_display = (
        "squad",
        "user",
        "status",
        "source",
        "jersey_number",
        "is_captain",
        "joined_at",
    )

    list_filter = (
        "status",
        "source",
    )

    search_fields = (
        "squad__name",
        "squad__organization__name",
        "user__username",
        "user__email",
    )

    list_select_related = ("squad", "user")

    ordering = ("-requested_at",)

    def get_readonly_fields(self, request, obj=None):
        return [
            field.name
            for field in SquadMember._meta.fields
            if field.name != "notes"
        ]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
