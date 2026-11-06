"""Response shapes for the members screens. Reads only; every rule is in the service."""

from rest_framework import serializers

from apps.organization.models import OrganizationInvite, OrganizationMember
from apps.accounts.services.account_deletion_service import mask_email


def _person(user):
    """{id, name, username, profile_photo}, the shape every row here shows."""
    profile = getattr(user, "profile", None)
    return {
        "id": str(user.id),
        "name": getattr(profile, "name", "") or user.username or "",
        "username": user.username or "",
        "profile_photo": getattr(profile, "profile_photo", "") or "",
    }


def _member_label(member):
    """{name, username} for "Added by" / "Invited by", or null."""
    if member is None:
        return None

    profile = getattr(member.user, "profile", None)
    return {
        "name": getattr(profile, "name", "") or member.user.username or "",
        "username": member.user.username or "",
    }


class OrganizationMemberSerializer(serializers.ModelSerializer):
    user = serializers.SerializerMethodField()
    added_by = serializers.SerializerMethodField()
    is_me = serializers.SerializerMethodField()

    class Meta:
        model = OrganizationMember
        fields = ("id", "user", "role", "joined_at", "added_by", "is_me")

    def get_user(self, obj):
        return _person(obj.user)

    def get_added_by(self, obj):
        return _member_label(obj.added_by)

    def get_is_me(self, obj):
        return obj.user_id == self.context["user_id"]


class PendingInviteSerializer(serializers.ModelSerializer):
    """
    An invite as the org's managers see it.

    An invite made by email shows only its masked address, even when the address
    turned out to belong to an account: showing the person would tell the inviter
    who has a Goatza account, which this list must never do.
    """

    invited_user = serializers.SerializerMethodField()
    email_masked = serializers.SerializerMethodField()
    invited_by = serializers.SerializerMethodField()

    class Meta:
        model = OrganizationInvite
        fields = (
            "id", "role", "invited_user", "email_masked", "invited_by",
            "created_at", "expires_at",
        )

    def get_invited_user(self, obj):
        if obj.email or obj.invited_user is None:
            return None
        return _person(obj.invited_user)

    def get_email_masked(self, obj):
        return mask_email(obj.email) if obj.email else None

    def get_invited_by(self, obj):
        return _member_label(obj.invited_by)


class MyInviteSerializer(serializers.ModelSerializer):
    """An invite as the invited person sees it."""

    organization = serializers.SerializerMethodField()
    invited_by = serializers.SerializerMethodField()

    class Meta:
        model = OrganizationInvite
        fields = (
            "id", "organization", "role", "invited_by", "created_at", "expires_at",
        )

    def get_organization(self, obj):
        organization = obj.organization
        profile = getattr(organization, "profile", None)
        return {
            "id": str(organization.id),
            "name": organization.name,
            "username": organization.username,
            "logo": getattr(profile, "logo", "") or "",
        }

    def get_invited_by(self, obj):
        return _member_label(obj.invited_by)


class InviteCreateSerializer(serializers.Serializer):
    """
    Shape only. Role and "exactly one of user_id / email" are the service's, which
    answers them with a code the client can show.
    """

    user_id = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    email = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    role = serializers.CharField(required=False, allow_blank=True)


class RoleChangeSerializer(serializers.Serializer):
    role = serializers.CharField(required=False, allow_blank=True)
