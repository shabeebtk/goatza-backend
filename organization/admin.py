from django.contrib import admin
from .models import (
    Organization,
    OrganizationProfile,
    OrganizationMember,
    OrganizationSport,
    OrganizationLocation,
)


class OrganizationProfileInline(admin.StackedInline):
    model = OrganizationProfile
    can_delete = False
    extra = 0

    fields = (
        "logo",
        "cover_image",
        "description",
        "website",
        "level",
        "followers_count",
        "posts_count",
    )

    readonly_fields = ("followers_count", "posts_count")



class OrganizationMemberInline(admin.TabularInline):
    model = OrganizationMember
    extra = 0
    autocomplete_fields = ("user",)
    fields = ("user", "role", "created_at")
    readonly_fields = ("created_at",)


class OrganizationSportInline(admin.TabularInline):
    model = OrganizationSport
    extra = 0
    autocomplete_fields = ("sport",)
    fields = ("sport", "is_primary")


class OrganizationLocationInline(admin.TabularInline):
    model = OrganizationLocation
    extra = 0

    fields = (
        "name",
        "city",
        "country_code",
        "is_primary",
        "latitude",
        "longitude",
    )



@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "name",
        "username",
        "type",
        "created_by",
        "is_verified",
        "is_active",
        "created_at",
    )

    list_filter = (
        "type",
        "is_verified",
        "is_active",
        "created_at",
    )

    search_fields = (
        "name",
        "username",
        "created_by__username",
        "created_by__email",
    )

    autocomplete_fields = ("created_by",)

    readonly_fields = ("created_at", "updated_at")

    ordering = ("-created_at",)

    list_select_related = ("created_by",)

    inlines = [
        OrganizationProfileInline,
        OrganizationMemberInline,
        OrganizationSportInline,
        OrganizationLocationInline,
    ]

    fieldsets = (
        ("Basic Info", {
            "fields": ("name", "username", "type")
        }),
        ("Status", {
            "fields": ("is_verified", "is_active")
        }),
        ("Meta", {
            "fields": ("created_by", "created_at", "updated_at")
        }),
    )




@admin.register(OrganizationProfile)
class OrganizationProfileAdmin(admin.ModelAdmin):
    list_display = (
        "organization",
        "level",
        "followers_count",
        "posts_count",
    )

    search_fields = ("organization__name",)

    list_select_related = ("organization",)


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