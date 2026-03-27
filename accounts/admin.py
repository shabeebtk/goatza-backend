from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django import forms
from accounts.models import User, UserProfile
import cloudinary.uploader


class UserProfileAdminForm(forms.ModelForm):
    upload_profile_photo = forms.ImageField(required=False)
    upload_cover_photo = forms.ImageField(required=False)

    class Meta:
        model = UserProfile
        fields = "__all__"

    def save(self, commit=True):
        instance = super().save(commit=False)

        profile_photo = self.cleaned_data.get("upload_profile_photo")
        cover_photo = self.cleaned_data.get("upload_cover_photo")

        # Upload profile photo
        if profile_photo:
            result = cloudinary.uploader.upload(
                profile_photo,
                folder=f"goatza/users/{instance.user_id}/profile"
            )
            instance.profile_photo = result.get("secure_url")

        # Upload cover photo
        if cover_photo:
            result = cloudinary.uploader.upload(
                cover_photo,
                folder=f"goatza/users/{instance.user_id}/cover"
            )
            instance.cover_photo = result.get("secure_url")

        if commit:
            instance.save()

        return instance
    

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
            "fields": ("is_email_verified", "is_phone_verified")
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
    form = UserProfileAdminForm
    
    list_display = ("id", "user", "name", "gender", "created_at")
    search_fields = ("user__email", "name")
    list_filter = ("gender",)

    readonly_fields = ("created_at", "updated_at")