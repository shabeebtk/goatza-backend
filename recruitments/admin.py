# recruitments/admin.py

from django.contrib import admin

from recruitments.models import (
    Recruitment,
    RecruitmentPosition,
    RecruitmentMedia,
    RecruitmentApplication,
    RecruitmentApplicationStatusHistory,
    RecruitmentQuestion,
    RecruitmentQuestionOption,
    RecruitmentApplicationAnswer,
)


# =========================================================
# INLINE ADMINS
# =========================================================

class RecruitmentPositionInline(admin.TabularInline):
    model = RecruitmentPosition
    extra = 0


class RecruitmentMediaInline(admin.TabularInline):
    model = RecruitmentMedia
    extra = 0


class RecruitmentQuestionOptionInline(admin.TabularInline):
    model = RecruitmentQuestionOption
    extra = 0


class RecruitmentQuestionInline(admin.StackedInline):
    model = RecruitmentQuestion
    extra = 0
    show_change_link = True


class RecruitmentApplicationInline(admin.TabularInline):
    model = RecruitmentApplication
    extra = 0

    readonly_fields = [
        "applicant",
        "status",
        "applied_at",
    ]

    can_delete = False


# =========================================================
# RECRUITMENT ADMIN
# =========================================================

@admin.register(Recruitment)
class RecruitmentAdmin(admin.ModelAdmin):

    list_display = [
        "title",
        "organization",
        "sport",
        "recruitment_type",
        "status",
        "visibility",
        "city",
        "applications_count",
        "is_featured",
        "published_at",
        "created_at",
    ]

    list_filter = [
        "recruitment_type",
        "status",
        "visibility",
        "sport",
        "gender",
        "is_featured",
        "is_paid",
        "created_at",
    ]

    search_fields = [
        "title",
        "organization__name",
        "organization__username",
        "city",
    ]

    readonly_fields = [
        "views_count",
        "applications_count",
        "shortlisted_count",
        "selected_count",
        "published_at",
        "created_at",
        "updated_at",
    ]

    autocomplete_fields = [
        "organization",
        "sport",
    ]

    list_select_related = [
        "organization",
        "sport",
    ]

    ordering = [
        "-created_at"
    ]

    inlines = [
        RecruitmentPositionInline,
        RecruitmentMediaInline,
        RecruitmentQuestionInline,
        RecruitmentApplicationInline,
    ]

    fieldsets = [

        ("Basic Information", {
            "fields": (
                "organization",
                "created_by_member",
                "sport",
                "title",
                "short_description",
                "description",
                "recruitment_type",
                "status",
                "visibility",
            )
        }),

        ("Requirements", {
            "fields": (
                "gender",
                "min_age",
                "max_age",
                "experience_level",
            )
        }),

        ("Event Details", {
            "fields": (
                "application_deadline",
                "event_date",
                "is_remote",
                "max_applications",
            )
        }),

        ("Payment", {
            "fields": (
                "is_paid",
                "fee_amount",
                "fee_currency",
                "payment_note",
            )
        }),

        ("Location", {
            "fields": (
                "location",
                "location_name",
                "city",
                "country_code",
                "latitude",
                "longitude",
            )
        }),

        ("Analytics", {
            "fields": (
                "views_count",
                "applications_count",
                "shortlisted_count",
                "selected_count",
            )
        }),

        ("Flags", {
            "fields": (
                "is_featured",
                "is_deleted",
            )
        }),

        ("Timestamps", {
            "fields": (
                "published_at",
                "created_at",
                "updated_at",
            )
        }),
    ]


# =========================================================
# RECRUITMENT APPLICATION ADMIN
# =========================================================

class RecruitmentApplicationAnswerInline(admin.TabularInline):
    model = RecruitmentApplicationAnswer
    extra = 0

    readonly_fields = [
        "question",
        "answer_text",
        "selected_option",
        "created_at",
    ]

    can_delete = False


class RecruitmentApplicationStatusHistoryInline(admin.TabularInline):
    model = RecruitmentApplicationStatusHistory
    extra = 0

    readonly_fields = [
        "from_status",
        "to_status",
        "changed_by",
        "note",
        "created_at",
    ]

    can_delete = False


@admin.register(RecruitmentApplication)
class RecruitmentApplicationAdmin(admin.ModelAdmin):

    list_display = [
        "id",
        "recruitment",
        "applicant",
        "status",
        "reviewed_by",
        "applied_at",
    ]

    list_filter = [
        "status",
        "applied_at",
        "updated_at",
    ]

    search_fields = [
        "applicant__username",
        "applicant__email",
        "recruitment__title",
    ]

    readonly_fields = [
        "applied_at",
        "updated_at",
    ]

    autocomplete_fields = [
        "reviewed_by",
    ]

    list_select_related = [
        "recruitment",
        "applicant",
        "reviewed_by",
    ]

    ordering = [
        "-applied_at"
    ]

    inlines = [
        RecruitmentApplicationAnswerInline,
        RecruitmentApplicationStatusHistoryInline,
    ]


# =========================================================
# QUESTION ADMIN
# =========================================================

@admin.register(RecruitmentQuestion)
class RecruitmentQuestionAdmin(admin.ModelAdmin):

    list_display = [
        "question",
        "recruitment",
        "field_type",
        "is_required",
        "display_order",
    ]

    list_filter = [
        "field_type",
        "is_required",
    ]

    search_fields = [
        "question",
        "recruitment__title",
    ]

    inlines = [
        RecruitmentQuestionOptionInline
    ]


# =========================================================
# MEDIA ADMIN
# =========================================================

@admin.register(RecruitmentMedia)
class RecruitmentMediaAdmin(admin.ModelAdmin):

    list_display = [
        "id",
        "recruitment",
        "media_type",
        "order",
        "created_at",
    ]

    list_filter = [
        "media_type",
    ]


# =========================================================
# STATUS HISTORY ADMIN
# =========================================================

@admin.register(RecruitmentApplicationStatusHistory)
class RecruitmentApplicationStatusHistoryAdmin(
    admin.ModelAdmin
):

    list_display = [
        "application",
        "from_status",
        "to_status",
        "changed_by",
        "created_at",
    ]

    list_filter = [
        "to_status",
        "created_at",
    ]

# =========================================================
# ANSWER ADMIN
# =========================================================

@admin.register(RecruitmentApplicationAnswer)
class RecruitmentApplicationAnswerAdmin(
    admin.ModelAdmin
):

    list_display = [
        "application",
        "question",
        "selected_option",
        "created_at",
    ]

    search_fields = [
        "application__id",
        "question__question",
    ]




