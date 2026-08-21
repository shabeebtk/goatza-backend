from django.contrib import admin

from usernames.models import UsernameRegistry


@admin.register(UsernameRegistry)
class UsernameRegistryAdmin(admin.ModelAdmin):
    list_display = ("username_lower", "user", "organization", "created_at")
    search_fields = ("username_lower",)
