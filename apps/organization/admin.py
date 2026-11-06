from django import forms
from django.contrib import admin
from .models import (
    Organization,
    OrganizationInvite,
    OrganizationProfile,
    OrganizationMember,
    OrganizationSport,
    OrganizationLocation,
)


class OrganizationProfileInline(admin.StackedInline):
    model = OrganizationProfile
    can_delete = False
    extra = 0

    fields = (
        "logo",
        "cover_image",
        "description",
        "website",
        "level",
        "followers_count",
        "posts_count",
    )

    readonly_fields = ("followers_count", "posts_count")



class OrganizationMemberInline(admin.TabularInline):
    """Read-only. Role and status change in the app; a row is added on its own page."""

    model = OrganizationMember
    extra = 0
    fields = ("user", "role", "status", "joined_at", "removed_at")
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_queryset(self, request):
        # History too. The default manager only returns active members.
        return OrganizationMember.all_objects.select_related("user")


class OrganizationSportInline(admin.TabularInline):
    model = OrganizationSport
    extra = 0
    autocomplete_fields = ("sport",)
    fields = ("sport", "is_primary")


class OrganizationLocationInline(admin.TabularInline):
    model = OrganizationLocation
    extra = 0

    fields = (
        "name",
        "city",
        "country_code",
        "is_primary",
        "latitude",
        "longitude",
    )



@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "name",
        "username",
        "type",
        "created_by",
        "is_verified",
        "is_active",
        "is_suspended",
        "created_at",
    )

    list_filter = (
        "type",
        "is_verified",
        "is_active",
        "is_suspended",
        "created_at",
    )

    # The un-suspend path. A moderation suspension is set by an action on a
    # Report; clearing it is a deliberate manual edit, and this is the only
    # place it can be done.
    list_editable = ("is_suspended",)

    search_fields = (
        "name",
        "username",
        "created_by__username",
        "created_by__email",
    )

    autocomplete_fields = ("created_by",)

    readonly_fields = ("created_at", "updated_at")

    ordering = ("-created_at",)

    list_select_related = ("created_by",)

    inlines = [
        OrganizationProfileInline,
        OrganizationMemberInline,
        OrganizationSportInline,
        OrganizationLocationInline,
    ]

    fieldsets = (
        ("Basic Info", {
            "fields": ("name", "username", "type")
        }),
        ("Status", {
            "fields": ("is_verified", "is_active")
        }),
        ("Meta", {
            "fields": ("created_by", "created_at", "updated_at")
        }),
    )




@admin.register(OrganizationProfile)
class OrganizationProfileAdmin(admin.ModelAdmin):
    list_display = (
        "organization",
        "level",
        "followers_count",
        "posts_count",
    )

    search_fields = ("organization__name",)

    list_select_related = ("organization",)


class OrganizationMemberAdminForm(forms.ModelForm):
    """
    Django's unique check reads the default manager, which only returns active
    members, so re-adding somebody removed would pass it and be a 500 from the
    database. (organization, user) is unique across the history too.
    """

    def clean(self):
        cleaned = super().clean()

        organization = cleaned.get("organization")
        user = cleaned.get("user")

        if organization and user:
            existing = OrganizationMember.all_objects.filter(
                organization=organization, user=user
            ).exclude(pk=self.instance.pk)

            if existing.exists():
                raise forms.ValidationError(
                    "This person already has a row in this organization, active "
                    "or former. Open that row, or invite them in the app to bring "
                    "them back."
                )

        return cleaned


@admin.register(OrganizationMember)
class OrganizationMemberAdmin(admin.ModelAdmin):
    form = OrganizationMemberAdminForm

    list_display = (
        "id",
        "organization",
        "user",
        "role",
        "status",
        "removed_by",
        "joined_at",
        "created_at",
    )

    list_filter = (
        "role",
        "status",
        "created_at",
    )

    search_fields = (
        "organization__name",
        "user__username",
        "user__email",
    )

    autocomplete_fields = ("organization", "user")

    readonly_fields = (
        "status", "created_at", "added_by", "removed_at", "removed_by",
    )

    list_select_related = ("organization", "user", "removed_by__user")

    ordering = ("-created_at",)

    def get_readonly_fields(self, request, obj=None):
        # Changes go through the app, which keeps the last-owner rule and closes
        # the person's sockets, and a row is one person's history at one org, so it
        # cannot be moved to another. A brand new row still needs all three.
        fields = super().get_readonly_fields(request, obj)
        return fields if obj is None else ("organization", "user", "role", *fields)

    def has_delete_permission(self, request, obj=None):
        # Removal goes through the app, which keeps the row, the status and who did
        # it. A deleted row would blank "changed by" on applicant timelines. This
        # also takes the bulk delete action off the list.
        return False

    def get_queryset(self, request):
        # History too. The default manager only returns active members, and a
        # removed member is exactly who somebody opens this page to look up.
        return OrganizationMember.all_objects.all()


@admin.register(OrganizationInvite)
class OrganizationInviteAdmin(admin.ModelAdmin):
    """
    Read-only. token_hash is never shown, and a row without a raw token is useless.
    Cancelling goes through the app too, which keeps who may do it, and an invite's
    row is the record of who was asked and by whom, so it is not deleted either.
    """

    list_display = (
        "id",
        "organization",
        "role",
        "invited_user",
        "email",
        "status",
        "expires_at",
        "created_at",
    )

    list_filter = ("status", "role", "created_at")

    search_fields = (
        "organization__name",
        "invited_user__username",
        "email",
    )

    fields = (
        "organization",
        "role",
        "invited_by",
        "invited_user",
        "email",
        "status",
        "expires_at",
        "responded_at",
        "accepted_member",
        "created_at",
    )

    readonly_fields = (
        "organization",
        "role",
        "invited_by",
        "invited_user",
        "email",
        "status",
        "expires_at",
        "responded_at",
        "accepted_member",
        "created_at",
    )

    list_select_related = ("organization", "invited_user")

    ordering = ("-created_at",)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False



@admin.register(OrganizationSport)
class OrganizationSportAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "organization",
        "sport",
        "is_primary",
    )

    list_filter = (
        "is_primary",
    )

    search_fields = (
        "organization__name",
        "sport__name",
    )

    autocomplete_fields = ("organization", "sport")

    list_select_related = ("organization", "sport")