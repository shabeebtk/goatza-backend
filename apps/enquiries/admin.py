"""
Enquiries in the Django admin: read-only, filtered by org and status, and the
phone MASKED in the list — staff browsing the list should not be reading
families' numbers. The detail page still shows it (support needs it).
"""

from django.contrib import admin

from apps.enquiries.models import Enquiry, EnquiryAlertState
from apps.orgpages.phones import mask_phone


@admin.register(Enquiry)
class EnquiryAdmin(admin.ModelAdmin):
    list_display = (
        "player_name",
        "organization",
        "status",
        "for_whom",
        "masked_phone",
        "program_name",
        "created_at",
    )
    list_filter = (
        "status",
        ("organization", admin.RelatedOnlyFieldListFilter),
    )
    search_fields = ("organization__name", "organization__username", "player_name")
    list_select_related = ("organization",)
    ordering = ("-created_at",)

    @admin.display(description="Phone")
    def masked_phone(self, obj):
        return mask_phone(obj.phone)

    def get_readonly_fields(self, request, obj=None):
        return [field.name for field in Enquiry._meta.fields]

    def has_add_permission(self, request):
        return False


@admin.register(EnquiryAlertState)
class EnquiryAlertStateAdmin(admin.ModelAdmin):
    list_display = ("organization", "last_email_at", "emails_day", "emails_today")
    search_fields = ("organization__name",)
    list_select_related = ("organization",)

    def get_readonly_fields(self, request, obj=None):
        return [field.name for field in EnquiryAlertState._meta.fields]

    def has_add_permission(self, request):
        return False
