import json

from django.contrib import admin, messages
from django.db.models import Count, IntegerField, OuterRef, Subquery, Value
from django.db.models.functions import Coalesce
from django.urls import NoReverseMatch, reverse
from django.utils.html import format_html

from moderation.services.report_services import ReportService

from .models import Block, Report


@admin.register(Block)
class BlockAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "blocker_display",
        "blocked_display",
        "created_at",
    )

    list_filter = (
        "created_at",
    )

    search_fields = (
        "blocker_user__username",
        "blocker_user__email",
        "blocked_user__username",
        "blocked_user__email",
        "blocker_org__name",
        "blocked_org__name",
    )

    autocomplete_fields = (
        "blocker_user",
        "blocked_user",
        "blocker_org",
        "blocked_org",
    )

    readonly_fields = ("created_at",)

    ordering = ("-created_at",)

    list_select_related = (
        "blocker_user",
        "blocked_user",
        "blocker_org",
        "blocked_org",
    )

    # Better display (human readable)
    def blocker_display(self, obj):
        if obj.blocker_user:
            return f"User: {obj.blocker_user.username or obj.blocker_user.id}"
        return f"Org: {obj.blocker_org.name if obj.blocker_org else obj.blocker_org_id}"

    blocker_display.short_description = "Blocker"

    def blocked_display(self, obj):
        if obj.blocked_user:
            return f"User: {obj.blocked_user.username or obj.blocked_user.id}"
        return f"Org: {obj.blocked_org.name if obj.blocked_org else obj.blocked_org_id}"

    blocked_display.short_description = "Blocked"


# ---------------------------------------------------------------------------
# REPORTS — the moderation queue
# ---------------------------------------------------------------------------
#
# admin.py IS the moderation tool for now (spec §2.5), so this list page is a
# real product surface rather than a debugging convenience. Two things follow
# from that:
#
#   * Everything a moderator needs to triage is in list_display — what was
#     reported, why, how many separate people said so — because opening rows
#     one at a time to find that out does not scale past a handful.
#   * The record itself is READ-ONLY. A report is evidence: someone's words
#     about someone else's content, plus a snapshot taken at the time. Only the
#     OUTCOME (status, resolution_note) is writable, and the normal way to
#     write it is an action, not the form.
#
# This stage is triage only. The enforcement actions — remove content, warn,
# suspend — land next, and they are why the transitions live in ReportService
# rather than here.


# The six target columns, in the order they are checked, with the label shown
# in the queue. Order matters only for readability: a row has exactly one
# non-null target (report_exactly_one_target).
TARGET_FIELDS = (
    ("reported_user", "User"),
    ("reported_org", "Organization"),
    ("reported_post", "Post"),
    ("reported_comment", "Comment"),
    ("reported_message", "Message"),
    ("reported_recruitment", "Recruitment"),
)


def _distinct_reporters_on(column):
    """
    Correlated subquery: how many DISTINCT reporter identities have ever
    reported the same thing this row points at.

    Distinct IDENTITIES via ``Coalesce(reporter_user, reporter_org)`` — one of
    the two is always NULL, and both hold UUIDv7s, so coalescing them yields
    one comparable identity column. Counting ROWS instead would be wrong the
    moment a report is dismissed and the same person files a fresh one.

    ANY status, deliberately: the signal a moderator wants from this column is
    "how many people have complained about this", and a dismissed pile is
    exactly the context that makes the seventh report interesting.

    When the outer row's column is NULL the inner filter matches nothing and
    the subquery yields NULL, which is what lets the six be COALESCEd into one
    number in ``get_queryset``.
    """
    return Subquery(
        Report.objects
        .filter(**{column: OuterRef(column)})
        .values(column)
        .annotate(n=Count(Coalesce("reporter_user", "reporter_org"), distinct=True))
        .values("n")[:1],
        output_field=IntegerField(),
    )


@admin.register(Report)
class ReportAdmin(admin.ModelAdmin):
    """
    The queue. Sorted priority-first, then newest — the two things that decide
    what a moderator looks at next.
    """

    list_display = (
        "target_label",
        "category",
        "is_priority",
        "reporters_on_target",
        "status",
        "created_at",
    )

    list_filter = (
        "status",
        "category",
        "is_priority",
        "created_at",
    )

    search_fields = (
        "reported_user__username",
        "reported_org__username",
        "reported_org__name",
        "reporter_user__username",
        "reporter_org__username",
        "reporter_org__name",
        "details",
    )

    ordering = ("-is_priority", "-created_at")

    date_hierarchy = "created_at"

    # The nested author paths matter as much as the direct FKs: target_label
    # renders "Post · @handle", and the handle lives one join further out. With
    # only the six direct FKs selected, a 100-row page costs 100 extra queries.
    list_select_related = (
        "reporter_user",
        "reporter_org",
        "reported_user",
        "reported_org",
        "reported_post",
        "reported_post__author_user",
        "reported_post__author_org",
        "reported_comment",
        "reported_comment__user",
        "reported_comment__organization",
        "reported_message",
        "reported_message__sender_user",
        "reported_message__sender_org",
        "reported_recruitment",
        "reported_recruitment__organization",
    )

    # Everything except the outcome. A report records what somebody said about
    # somebody else's content — an admin who can retype the category, the
    # details or the target has a record that no longer proves anything.
    readonly_fields = (
        "reporter_display",
        "category",
        "details",
        "created_at",
        "target_label",
        "reported_user",
        "reported_org",
        "reported_post",
        "reported_comment",
        "reported_message",
        "reported_recruitment",
        "snapshot_pretty",
        "reviewed_by",
        "reviewed_at",
        "action_taken",
    )

    fieldsets = (
        ("Report", {
            "fields": (
                "reporter_display",
                "category",
                "details",
                "created_at",
            ),
        }),
        ("Target", {
            "classes": ("collapse",),
            "description": (
                "Exactly one is set. A NULL here means the row was hard-deleted "
                "after the report — the snapshot below is then the only record."
            ),
            "fields": (
                "target_label",
                "reported_user",
                "reported_org",
                "reported_post",
                "reported_comment",
                "reported_message",
                "reported_recruitment",
            ),
        }),
        ("Snapshot", {
            "description": (
                "Captured when the report was filed and never updated — edits "
                "to the content after the fact cannot rewrite it."
            ),
            "fields": ("snapshot_pretty",),
        }),
        ("Resolution", {
            "description": (
                "Reviewer, timestamp and action are set by the list-page "
                "actions, not typed here."
            ),
            "fields": (
                "status",
                "reviewed_by",
                "reviewed_at",
                "action_taken",
                "resolution_note",
            ),
        }),
    )

    # Triage first, then the three that change something in the product,
    # then the two suspensions. Order is the order a moderator escalates.
    actions = (
        "mark_reviewing",
        "dismiss",
        "remove_content",
        "warn_author",
        "suspend_user",
        "suspend_organization",
    )

    # Reports come from POST /moderation/report. There is no such thing as a
    # hand-written one, and with the reporter columns read-only the add form
    # could only ever produce a row that violates reporter_user_or_org.
    def has_add_permission(self, request):
        return False

    # =================================================================
    # QUERYSET
    # =================================================================

    def get_queryset(self, request):
        """
        One extra annotation, no extra queries per row.

        Six correlated subqueries collapse to one number — only the branch
        matching this row's non-null target returns anything, and each is an
        indexed lookup on the per-target indexes the model declares. The
        alternative (counting in Python per row) is the N+1 this exists to
        avoid.
        """
        queryset = super().get_queryset(request)

        return queryset.annotate(
            reporters_count=Coalesce(
                *[_distinct_reporters_on(column) for column, _ in TARGET_FIELDS],
                Value(0),
                output_field=IntegerField(),
            )
        )

    # =================================================================
    # DISPLAY
    # =================================================================

    @staticmethod
    def _target_of(obj):
        """``(field_name, label, instance)`` for the one non-null target."""
        for field, label in TARGET_FIELDS:
            instance = getattr(obj, field, None)
            if instance is not None:
                return field, label, instance

        return None, None, None

    @staticmethod
    def _handle_for(field, instance):
        """
        The @handle shown beside the target's kind.

        For an account it is the account's own; for a piece of content it is
        the AUTHOR's, because "Post · @someone" is what a moderator is actually
        deciding about — a bare post id says nothing.
        """
        if field == "reported_user":
            return instance.username

        if field == "reported_org":
            return instance.username

        if field == "reported_post":
            owner = instance.author_user or instance.author_org
        elif field == "reported_comment":
            owner = instance.user or instance.organization
        elif field == "reported_message":
            owner = instance.sender_user or instance.sender_org
        else:
            owner = instance.organization

        return getattr(owner, "username", None) if owner else None

    @admin.display(description="Target")
    def target_label(self, obj):
        """
        ``Post · @handle`` linking to the target's own admin page.

        Falls back to plain text if that model has no admin registration —
        a missing link is a cosmetic loss, a NoReverseMatch is a 500 on the
        queue itself.
        """
        field, label, instance = self._target_of(obj)

        if instance is None:
            # SET_NULL fired: the content was hard-deleted after the report.
            # This is exactly the case content_snapshot exists for.
            # The label is passed as an ARGUMENT, not baked into the format
            # string: format_html refuses a call with no interpolation
            # arguments (Django 5+), and routing every value through a
            # placeholder is the habit that keeps the escaping honest.
            return format_html(
                '<span style="color:#999">{}</span>', "[deleted] — see snapshot"
            )

        handle = self._handle_for(field, instance)
        text = f"{label} · @{handle}" if handle else f"{label} · {instance.pk}"

        meta = instance._meta
        try:
            url = reverse(
                f"admin:{meta.app_label}_{meta.model_name}_change",
                args=[instance.pk],
            )
        except NoReverseMatch:
            return text

        return format_html('<a href="{}">{}</a>', url, text)

    @admin.display(description="Reporters", ordering="reporters_count")
    def reporters_on_target(self, obj):
        """Distinct identities who have reported this target — the pile size."""
        return obj.reporters_count

    @admin.display(description="Reporter")
    def reporter_display(self, obj):
        """The reporting identity, linked. Mirrors BlockAdmin's *_display pair."""
        entity = obj.reporter_user or obj.reporter_org

        if entity is None:
            return "—"

        kind = "User" if obj.reporter_user_id else "Org"
        name = entity.username or getattr(entity, "name", "") or entity.pk

        meta = entity._meta
        try:
            url = reverse(
                f"admin:{meta.app_label}_{meta.model_name}_change", args=[entity.pk]
            )
        except NoReverseMatch:
            return f"{kind}: {name}"

        return format_html('<a href="{}">{}: {}</a>', url, kind, name)

    @admin.display(description="Snapshot")
    def snapshot_pretty(self, obj):
        """
        content_snapshot as indented JSON.

        ``format_html`` and never ``mark_safe``: every value in here is text a
        reported account wrote, and the whole point of rendering it is that a
        moderator reads hostile content. Escaping is not optional — the
        placeholder is what escapes it.
        """
        if not obj.content_snapshot:
            return "—"

        pretty = json.dumps(
            obj.content_snapshot, indent=2, ensure_ascii=False, sort_keys=True
        )

        return format_html(
            '<pre style="white-space:pre-wrap;word-break:break-word;'
            'background:#f6f6f6;border:1px solid #ddd;border-radius:4px;'
            'padding:12px;max-height:480px;overflow:auto">{}</pre>',
            pretty,
        )

    # =================================================================
    # ACTIONS
    # =================================================================
    #
    # Thin on purpose: loop, delegate to ReportService, count, report. Every
    # rule about which transitions are legal lives in the service, because the
    # enforcement actions arriving next have to resolve reports the same way.

    def _run(self, request, queryset, operation, verb):
        """Apply ``operation`` to each row and tell the moderator what moved."""
        changed = 0

        for report in queryset:
            if operation(report):
                changed += 1

        skipped = len(queryset) - changed

        self.message_user(
            request,
            f"{changed} {verb}, {skipped} skipped.",
            messages.SUCCESS if changed else messages.WARNING,
        )

    @admin.action(description="Mark as reviewing")
    def mark_reviewing(self, request, queryset):
        self._run(
            request,
            queryset,
            lambda report: ReportService.mark_reviewing(report, request.user),
            "marked reviewing",
        )

    @admin.action(description="Dismiss")
    def dismiss(self, request, queryset):
        self._run(
            request,
            queryset,
            lambda report: ReportService.dismiss(report, request.user),
            "dismissed",
        )

    def _enforce(self, request, queryset, operation, verb):
        """
        Apply an enforcement operation and report done / skipped / refused.

        Three outcomes, not two. ``(False, reason)`` means the moderator picked
        an action this target cannot take — "Suspend user" on a club — and that
        has to read differently from "already handled", or a batch that did
        nothing at all looks like a batch that was already done.
        """
        done = 0
        skipped = 0
        refusals = []

        for report in queryset:
            moved, error = operation(report)

            if moved:
                done += 1
            elif error:
                refusals.append(error)
            else:
                skipped += 1

        parts = [f"{done} {verb}"]
        if skipped:
            parts.append(f"{skipped} skipped")
        if refusals:
            parts.append(f"{len(refusals)} not applicable")

        self.message_user(
            request,
            ", ".join(parts) + ".",
            messages.SUCCESS if done else messages.WARNING,
        )

        # One line per distinct reason, so a mixed selection says WHY rather
        # than leaving the moderator to guess which rows were refused.
        for reason in dict.fromkeys(refusals):
            self.message_user(request, reason, messages.WARNING)

    @admin.action(description="Remove reported content")
    def remove_content(self, request, queryset):
        self._enforce(
            request,
            queryset,
            lambda report: ReportService.remove_content(report, request.user),
            "removed",
        )

    @admin.action(description="Warn author")
    def warn_author(self, request, queryset):
        self._enforce(
            request,
            queryset,
            lambda report: ReportService.warn_author(report, request.user),
            "warned",
        )

    @admin.action(description="Suspend user account")
    def suspend_user(self, request, queryset):
        self._enforce(
            request,
            queryset,
            lambda report: ReportService.suspend_user(report, request.user),
            "suspended",
        )

    @admin.action(description="Suspend organization")
    def suspend_organization(self, request, queryset):
        self._enforce(
            request,
            queryset,
            lambda report: ReportService.suspend_organization(report, request.user),
            "suspended",
        )
