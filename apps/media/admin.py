"""
Media jobs, read-only — where an operator reads ``error_detail`` (ffmpeg's
stderr, a traceback), which no API ever returns. Nothing here is editable: a
job's state belongs to the worker and the sweeper.
"""

from django.contrib import admin

from .models import MediaJob, PendingPublication


class ReadOnlyAdmin(admin.ModelAdmin):
    def get_readonly_fields(self, request, obj=None):
        return [field.name for field in self.model._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        # Viewing still works; saving does not.
        return False


@admin.register(PendingPublication)
class PendingPublicationAdmin(ReadOnlyAdmin):
    list_display = (
        "id", "target", "status", "error_code", "result_id", "created_at",
        "published_at",
    )
    list_filter = ("status", "target", "error_code")
    search_fields = ("id", "result_id")
    ordering = ("-created_at",)
    raw_id_fields = ("owner_user", "owner_org", "created_by")


@admin.register(MediaJob)
class MediaJobAdmin(ReadOnlyAdmin):
    list_display = (
        "id", "status", "kind", "purpose", "error_code", "attempts",
        "source_size_bytes", "created_at", "finished_at",
    )
    list_filter = ("status", "kind", "purpose", "error_code")
    search_fields = ("id", "source_key", "output_key")
    ordering = ("-created_at",)
    raw_id_fields = ("owner_user", "owner_org", "created_by")
