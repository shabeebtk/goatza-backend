"""
The consent records, for looking at and — in the events' case — for nothing
else.

``GuardianConsentEvent`` IS FULLY READ-ONLY here: every field is in
``readonly_fields``, there is no add form and there is no delete. That is the
append-only rule from the model docstring, enforced at the one surface where a
human could otherwise break it. An admin who can retype a consent event has an
audit table that proves nothing, and "fixing" a wrong row is exactly the thing
that must not be possible — the correction is a NEW event, written by the
service, the same way a withdrawal is.

``Guardian`` stays editable, because it is a contact rather than evidence: a
parent who mistypes their phone number needs it corrected, and correcting it
changes nothing about what they already agreed to.
"""

from django.contrib import admin
from django.db.models import Count

from .models import Guardian, GuardianConsentEvent


@admin.register(Guardian)
class GuardianAdmin(admin.ModelAdmin):
    list_display = ("name", "email", "phone", "children_count", "linked_user")

    search_fields = ("name", "email", "phone")

    # EmptyFieldListFilter, not a plain FK filter: "is this contact also an
    # account" is the useful question, and the plain version would render every
    # user in the database as a <select>.
    list_filter = (("linked_user", admin.EmptyFieldListFilter),)

    # A user picker, not that same <select> on the form. UserAdmin defines
    # search_fields, which is what autocomplete needs.
    autocomplete_fields = ("linked_user",)

    list_select_related = ("linked_user",)

    def get_queryset(self, request):
        """Annotated rather than counted per row — otherwise the list page is an N+1."""
        return super().get_queryset(request).annotate(
            children=Count("events__child", distinct=True)
        )

    @admin.display(description="Children", ordering="children")
    def children_count(self, obj):
        """
        How many distinct children this contact has events for — the column
        that makes the sibling case visible, which is the whole reason a
        guardian is a row of its own rather than three columns on a profile.
        """
        return obj.children


@admin.register(GuardianConsentEvent)
class GuardianConsentEventAdmin(admin.ModelAdmin):
    list_display = (
        "created_at",
        "child",
        "guardian",
        "event_type",
        "method",
        "level",
        "notice_version",
    )

    list_filter = ("event_type", "method", "level", "created_at")

    search_fields = (
        "child__username",
        "child__email",
        "guardian__name",
        "guardian__email",
        "guardian__phone",
    )

    ordering = ("-created_at",)

    date_hierarchy = "created_at"

    list_select_related = ("child", "guardian")

    # Everything. See the module docstring — this is the evidence table.
    readonly_fields = (
        "guardian",
        "child",
        "event_type",
        "method",
        "level",
        "notice_version",
        "parent_name_given",
        "parent_birthdate_given",
        "token_hash",
        "token_expires_at",
        "ip_address",
        "user_agent",
        "created_at",
    )

    # Events are written by the consent service and by nothing else. With every
    # field read-only the add form could only ever produce a blank row anyway.
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    # Append-only means append-only. Deleting a row here would destroy the
    # answer to "was this child's guardian ever asked", which is the one
    # question this table exists to answer.
    def has_delete_permission(self, request, obj=None):
        return False
