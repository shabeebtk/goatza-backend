"""
The page in the Django admin: READ-MOSTLY.

Every rule lives in the services (the PageSettings lock, page.manage, phone
normalization, the limits, deleting replaced photos from storage), and none of
it runs here. So the page's own rows are read-only and cannot be added here.
They CAN be deleted: nothing here keeps a counter, and a row the admin may not
delete would stop the admin hard-deleting the Organization or User it
cascades from (it checks every related row first). A photo deleted here stays
in storage, which is the price of that.

The one exception is the facilities catalog, which is seeded DATA: editable,
and a re-run of seed_facility_catalog resets the fields it seeds.
"""

from django.contrib import admin

from apps.orgpages.models import (
    Coach,
    FacilityItem,
    FacilityPhoto,
    FeaturedPlayer,
    LocationFacilities,
    PageSettings,
    Program,
)


class ReadOnlyAdmin(admin.ModelAdmin):
    def get_readonly_fields(self, request, obj=None):
        return [field.name for field in self.model._meta.fields] + [
            field.name for field in self.model._meta.many_to_many
        ]

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(FacilityItem)
class FacilityItemAdmin(admin.ModelAdmin):
    list_display = ("label", "key", "group", "order", "is_active")
    list_filter = ("group", "is_active")
    search_fields = ("key", "label")
    ordering = ("group", "order", "label")


@admin.register(PageSettings)
class PageSettingsAdmin(ReadOnlyAdmin):
    list_display = (
        "organization",
        "enquiry_form_enabled",
        "whatsapp_enabled",
        "call_enabled",
        "email_enabled",
        "updated_at",
    )
    list_filter = ("enquiry_form_enabled",)
    search_fields = ("organization__name", "organization__username")
    list_select_related = ("organization",)
    ordering = ("-updated_at",)


@admin.register(Program)
class ProgramAdmin(ReadOnlyAdmin):
    list_display = (
        "name", "organization", "sport", "level", "fee_mode", "spots", "order",
    )
    list_filter = ("level", "fee_mode", "spots")
    search_fields = ("name", "organization__name", "organization__username")
    list_select_related = ("organization", "sport")
    ordering = ("organization", "order")


@admin.register(LocationFacilities)
class LocationFacilitiesAdmin(ReadOnlyAdmin):
    list_display = ("location", "updated_at")
    search_fields = ("location__organization__name", "location__city")
    list_select_related = ("location__organization",)


@admin.register(FacilityPhoto)
class FacilityPhotoAdmin(ReadOnlyAdmin):
    list_display = ("location", "order", "created_at")
    search_fields = ("location__organization__name",)
    list_select_related = ("location__organization",)


@admin.register(Coach)
class CoachAdmin(ReadOnlyAdmin):
    list_display = ("__str__", "organization", "role", "is_visible", "order")
    list_filter = ("role", "is_visible")
    search_fields = ("name", "organization__name", "organization__username")
    list_select_related = ("organization",)


@admin.register(FeaturedPlayer)
class FeaturedPlayerAdmin(ReadOnlyAdmin):
    list_display = ("user", "organization", "highlight", "order", "created_at")
    search_fields = ("organization__name", "user__username")
    list_select_related = ("organization", "user")
