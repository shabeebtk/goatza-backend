import logging

from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django import forms
from apps.accounts.models import User, UserProfile
from apps.guardians.services.consent_service import (
    clear_pending_for_adult,
    ensure_pending_for_minor,
)

logger = logging.getLogger(__name__)

WITHDRAWN_NEEDS_REVIEW_MESSAGE = (
    "A parent withdrew permission for this account, so it stays locked even with "
    "an adult date. Review it by hand."
)


def apply_birthdate_correction(request, profile):
    """
    Support's route for a wrong date of birth, which the app itself refuses once
    onboarding is done. Records who did it: a date here decides which
    child-protection rules hold this person. The date itself stays out of the log.

    It runs both ways. A minor's date locks the account, by the same rule as
    everywhere else. An adult's date lifts a lock that came from the wrong date,
    or the purge would delete the account once the request went stale. A
    withdrawal stays, because that was a parent's decision, and staff are told.

    A cleared date is left alone: unknown reads as a minor, and a legacy account
    with no date on file was deliberately never locked for that.
    """
    user = User.objects.select_related("profile").get(pk=profile.user_id)
    status_before = user.guardian_consent_status

    if profile.birthdate is not None:
        ensure_pending_for_minor(user)
        clear_pending_for_adult(user, staff_id=request.user.pk)

        if (
            not user.is_minor
            and user.guardian_consent_status == User.GuardianConsentStatus.WITHDRAWN
        ):
            messages.warning(request, WITHDRAWN_NEEDS_REVIEW_MESSAGE)

    logger.info(
        "Admin birthdate change | user=%s | staff=%s | status=%s -> %s",
        user.pk, request.user.pk, status_before, user.guardian_consent_status,
    )


# -----------------------------
# Custom User Creation Form
# -----------------------------
class UserCreationForm(forms.ModelForm):
    password = forms.CharField(widget=forms.PasswordInput)

    class Meta:
        model = User
        fields = ("email", "phone", "username", "role")

    def clean(self):
        cleaned_data = super().clean()
        email = cleaned_data.get("email")
        phone = cleaned_data.get("phone")

        if not email and not phone:
            raise forms.ValidationError("Either email or phone is required")

        return cleaned_data

    def save(self, commit=True):
        user = super().save(commit=False)
        user.set_password(self.cleaned_data["password"])
        if commit:
            user.save()
        return user


# -----------------------------
# Custom User Change Form
# -----------------------------
class UserChangeForm(forms.ModelForm):
    class Meta:
        model = User
        fields = "__all__"


# -----------------------------
# Inline Profile (VERY USEFUL)
# -----------------------------
class UserProfileInline(admin.StackedInline):
    model = UserProfile
    can_delete = False
    extra = 0


# -----------------------------
# User Admin
# -----------------------------
@admin.register(User)
class UserAdmin(BaseUserAdmin):
    form = UserChangeForm
    add_form = UserCreationForm

    list_display = (
        "id",
        "email",
        "phone",
        "username",
        "role",
        "is_active",
        "is_staff",
        "created_at",
    )

    list_filter = ("role", "is_active", "is_staff", "is_email_verified")

    search_fields = ("email", "phone", "username")
    ordering = ("-created_at",)

    readonly_fields = ("created_at", "updated_at")

    inlines = [UserProfileInline]

    # -------------------------
    # Detail View
    # -------------------------
    fieldsets = (
        ("Basic Info", {
            "fields": ("email", "phone", "username", "password")
        }),
        ("Role & Status", {
            "fields": ("role", "is_active", "is_staff", "is_superuser")
        }),
        ("Verification", {
            "fields": ("is_email_verified", "is_phone_verified", "is_onboarding_completed")
        }),
        ("Permissions", {
            "fields": ("groups", "user_permissions"),
        }),
        ("Timestamps", {
            "fields": ("created_at", "updated_at"),
        }),
    )

    # -------------------------
    # Create User View
    # -------------------------
    add_fieldsets = (
        (None, {
            "classes": ("wide",),
            "fields": ("email", "phone", "username", "role", "password"),
        }),
    )

    def save_formset(self, request, form, formset, change):
        """The profile inline is the other place staff can change a birthdate."""
        super().save_formset(request, form, formset, change)

        if formset.model is not UserProfile:
            return

        for inline_form in formset.forms:
            if "birthdate" in inline_form.changed_data:
                apply_birthdate_correction(request, inline_form.instance)


# -----------------------------
# User Profile Admin
# -----------------------------
@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    # No upload widgets here any more.
    #
    # They wrote straight to the old provider from the admin process, into a
    # `goatza/users/<id>/...` folder that never matched the app's own scheme,
    # and never set the paired *_public_id — so anything uploaded this way was
    # unreferenced and undeletable from day one. Media now goes through the
    # presigned-PUT flow in the app, which is the only path that produces a
    # matching URL/key pair. `profile_photo` and `cover_photo` remain editable
    # as plain URL fields.
    list_display = ("id", "user", "name", "gender", "created_at")
    search_fields = ("user__email", "name")
    list_filter = ("gender",)

    readonly_fields = ("created_at", "updated_at")

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)

        if "birthdate" in form.changed_data:
            apply_birthdate_correction(request, obj)