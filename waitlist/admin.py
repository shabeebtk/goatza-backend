import csv

from django.contrib import admin
from django.http import HttpResponse
from django.utils import timezone

from waitlist.models import PlayerSignup
from waitlist.selectors.signup_selectors import display_number, is_founding

# The columns the CSV export writes, in order. Matches what the list view
# shows plus the contact fields and my notes — the export is for working the
# list offline (calling players, splitting it by city), so it carries the
# things the on-screen table deliberately leaves out.
#
# The full location block is here, coordinates included: this file never leaves
# my machine, and "who is within an hour of this trial" is a question only the
# coordinates can answer.
EXPORT_FIELDS = (
    "signup_number",
    "ref_code",
    "name",
    "phone",
    "email",
    "instagram",
    "date_of_birth",
    "location_name",
    "city",
    "state",
    "country_code",
    "latitude",
    "longitude",
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
    """
    The waitlist as I actually work it.

    This is the ONE surface that shows both numbers. Everything a client can
    see carries the display number (real + WAITLIST_DISPLAY_OFFSET); here the
    stored ``signup_number`` sits next to it, because a row is found by the
    stored one and a player quotes the displayed one.
    """

    list_display = (
        "signup_number",
        "public_number",
        "name",
        "phone",
        "city",
        "country_code",
        "position",
        "level",
        "age",
        "founding",
        "instagram",
        "source",
        "created_at",
    )

    list_filter = (
        "country_code",
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
        "city",
        "location_name",
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

    def public_number(self, obj):
        """
        What this player was told they are — ``signup_number`` plus the offset.

        Computed, never stored and never editable: it is a view of the column
        two cells to the left, and a second source for it is a second thing to
        keep in sync. ``ref_code`` is the one place this number was frozen into
        the database, which is why the offset cannot move after go-live.
        """
        return f"#{display_number(obj.signup_number)}"

    public_number.short_description = "Public #"
    public_number.admin_order_field = "signup_number"

    def founding(self, obj):
        """
        Whether this player is in the founding cohort — display number <= goal.

        Also computed rather than a column. A stored flag would have to be
        backfilled every time the goal moved, and would then disagree with the
        badge on a card that is generated from the same rule at read time.
        """
        return is_founding(obj.signup_number)

    founding.short_description = "Founding"
    founding.boolean = True

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
