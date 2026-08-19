import csv

from django.contrib import admin
from django.http import HttpResponse
from django.utils import timezone

from waitlist.models import PlayerSignup

# The columns the CSV export writes, in order. Matches what the list view
# shows plus the contact fields and my notes — the export is for working the
# list offline (calling players, splitting it by district), so it carries the
# things the on-screen table deliberately leaves out.
EXPORT_FIELDS = (
    "signup_number",
    "ref_code",
    "name",
    "phone",
    "email",
    "instagram",
    "date_of_birth",
    "district",
    "state",
    "sport",
    "position",
    "level",
    "club_or_academy",
    "source",
    "notes",
    "created_at",
)


@admin.register(PlayerSignup)
class PlayerSignupAdmin(admin.ModelAdmin):
    list_display = (
        "signup_number",
        "name",
        "phone",
        "district",
        "position",
        "level",
        "age",
        "instagram",
        "source",
        "created_at",
    )

    list_filter = (
        "district",
        "position",
        "level",
        "sport",
        "created_at",
    )

    search_fields = (
        "name",
        "phone",
        "email",
        "instagram",
        "ref_code",
        "club_or_academy",
    )

    # Assigned by PlayerSignupService, and public once assigned — editing a
    # number here would silently break somebody's shared card.
    readonly_fields = (
        "signup_number",
        "ref_code",
        "created_at",
    )

    ordering = ("-created_at",)

    actions = ("export_as_csv",)

    def age(self, obj):
        """
        Years old today, or "-" when no date of birth was given.

        Computed rather than stored: an age column would be wrong the day after
        it was written. The ``(m, d) < (m, d)`` comparison subtracts the year
        this player has not had their birthday in yet.
        """
        if not obj.date_of_birth:
            return "-"

        today = timezone.localdate()
        born = obj.date_of_birth

        return (
            today.year
            - born.year
            - ((today.month, today.day) < (born.month, born.day))
        )

    age.short_description = "Age"

    @admin.action(description="Export selected as CSV")
    def export_as_csv(self, request, queryset):
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = (
            'attachment; filename="goatza_waitlist.csv"'
        )

        # BOM first. Without it Excel reads the file as the local codepage and
        # mangles every non-ASCII name in the list.
        response.write("﻿")

        writer = csv.writer(response)
        writer.writerow(EXPORT_FIELDS)

        for signup in queryset.order_by("signup_number"):
            writer.writerow(
                [getattr(signup, field) for field in EXPORT_FIELDS]
            )

        return response
