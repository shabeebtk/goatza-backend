from django.contrib import admin
from careers.models import CareerEntry


@admin.register(CareerEntry)
class CareerEntryAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "organization_name",
        "title",
        "sport",
        "verification_status",
        "source",
        "is_current",
    )

    list_filter = (
        "verification_status",
        "source",
        "entry_type",
    )

    search_fields = (
        "title",
        "organization_name",
        "user__username",
        "user__email",
        "organization__name",
    )

    autocomplete_fields = (
        "user",
        "organization",
        "sport",
        "verified_by",
        "positions",
    )

    # RecruitmentApplication admin exposes search_fields, but the entry is
    # linked by the pipeline, not picked by hand.
    raw_id_fields = (
        "recruitment_application",
    )

    readonly_fields = (
        "created_at",
        "updated_at",
    )

    list_select_related = (
        "user",
        "sport",
    )

    ordering = ("-start_date", "-created_at")
