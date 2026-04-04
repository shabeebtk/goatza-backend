from django.contrib import admin
from .models import Organization, OrganizationMember, OrganizationSport


# =========================
# 🔹 INLINE: MEMBERS
# =========================
class OrganizationMemberInline(admin.TabularInline):
    model = OrganizationMember
    extra = 0
    autocomplete_fields = ("user",)
    fields = ("user", "role", "created_at")
    readonly_fields = ("created_at",)


# =========================
# 🔹 INLINE: SPORTS
# =========================
class OrganizationSportInline(admin.TabularInline):
    model = OrganizationSport
    extra = 0
    autocomplete_fields = ("sport",)
    fields = ("sport", "is_primary")


# =========================
# 🔹 ORGANIZATION ADMIN
# =========================
@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "name",
        "type",
        "level",
        "created_by",
        "is_verified",
        "is_active",
        "created_at",
    )

    list_filter = (
        "type",
        "level",
        "is_verified",
        "is_active",
        "created_at",
    )

    search_fields = (
        "name",
        "slug",
        "created_by__username",
        "created_by__email",
    )

    prepopulated_fields = {"slug": ("name",)}

    autocomplete_fields = ("created_by",)

    readonly_fields = ("created_at", "updated_at")

    ordering = ("-created_at",)

    list_select_related = ("created_by",)

    inlines = [
        OrganizationMemberInline,
        OrganizationSportInline,
    ]

    fieldsets = (
        ("Basic Info", {
            "fields": ("name", "slug", "type", "level")
        }),
        ("Media", {
            "fields": ("logo", "cover_image")
        }),
        ("Details", {
            "fields": ("description", "website")
        }),
        ("Status", {
            "fields": ("is_verified", "is_active")
        }),
        ("Meta", {
            "fields": ("created_by", "created_at", "updated_at")
        }),
    )


# =========================
# 🔹 ORGANIZATION MEMBER ADMIN
# =========================
@admin.register(OrganizationMember)
class OrganizationMemberAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "organization",
        "user",
        "role",
        "created_at",
    )

    list_filter = (
        "role",
        "created_at",
    )

    search_fields = (
        "organization__name",
        "user__username",
        "user__email",
    )

    autocomplete_fields = ("organization", "user")

    readonly_fields = ("created_at",)

    list_select_related = ("organization", "user")

    ordering = ("-created_at",)


# =========================
# 🔹 ORGANIZATION SPORT ADMIN
# =========================
@admin.register(OrganizationSport)
class OrganizationSportAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "organization",
        "sport",
        "is_primary",
    )

    list_filter = (
        "is_primary",
    )

    search_fields = (
        "organization__name",
        "sport__name",
    )

    autocomplete_fields = ("organization", "sport")

    list_select_related = ("organization", "sport")