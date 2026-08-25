from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django import forms
from accounts.models import User, UserProfile


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