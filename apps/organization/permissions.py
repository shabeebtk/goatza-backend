"""
What a member of an organization may do — THE one place that is written down.

Before this file every feature kept its own tuple of roles (PRIVACY_ROLES,
BLOCK_ROLES, two REVIEWER_ROLES…). A permission here is a named capability and
a role is only a set of defaults, so a later "let this coach post announcements"
is one row in ``permission_overrides`` plus a screen, and nothing else changes.

ADDING A PERMISSION: a member of ``OrgPermission``, the roles that get it in
``ROLE_PERMISSIONS``, a sentence in ``DENIED_MESSAGES``, and the same name in the
frontend's ``orgPermissions.ts``. Then gate with ``require_org_permission`` —
never compare ``member.role``.

The one exception is ownership itself: ``account_deletion_service`` and the
last-owner rules in ``OrganizationMemberService`` ask "is this person an owner",
because that is a question about who holds the org, not what somebody may do.

This module imports no models at the top: ``apps.organization.models`` imports
``validate_permission_overrides`` from here for its field validator.
"""

from django.core.exceptions import ValidationError
from django.db import models
from rest_framework.exceptions import PermissionDenied


class OrgPermission(models.TextChoices):
    MEMBERS_VIEW = "members.view", "See who is in the organization"
    MEMBERS_MANAGE = "members.manage", "Invite, remove and change members"
    # The logged-out visibility of the org's public profile.
    ORG_PRIVACY = "org.privacy", "Change the public profile setting"
    # Speaking and acting for the club on a recruitment. One permission because
    # today it is one gate — AnnouncementService.require_reviewer: announcements,
    # the delete action (RecruitmentDeleteCheckAPIView's can_delete and
    # RecruitmentService.require_deleter) and the applicant export. Direct
    # messages to applicants are deliberately NOT behind it: any member may send
    # one (AnnouncementService.create_direct), and they still may.
    RECRUITMENTS_SPEAK = "recruitments.speak", "Speak for the org on recruitments"
    # Careers + achievements: a verification is the club standing behind a claim.
    VERIFICATIONS_REVIEW = "verifications.review", "Verify careers and achievements"
    ORG_BLOCK = "org.block", "Block accounts on behalf of the org"
    # Who gets the org's pushes and transactional emails. An org has no device
    # of its own, only members.
    ORG_RECEIVE_ALERTS = "org.receive_alerts", "Receive the org's alerts"


# Keyed by role VALUE, not OrganizationMember.Role, for the import reason above.
# Coach and staff read the list and nothing else here: this equals behaviour
# from before permissions existed, when every one of those lists was
# owner + admin and admin became owner.
ROLE_PERMISSIONS = {
    "owner": frozenset(OrgPermission.values),
    "coach": frozenset({OrgPermission.MEMBERS_VIEW.value}),
    "staff": frozenset({OrgPermission.MEMBERS_VIEW.value}),
}

OWNER_ROLE = "owner"

NOT_ORG_ACTOR_MESSAGE = "Switch to your organization account to do this."

DENIED_MESSAGES = {
    OrgPermission.MEMBERS_VIEW.value: (
        "You don't have access to this organization's members."
    ),
    OrgPermission.MEMBERS_MANAGE.value: (
        "Only the organization's owners can manage members."
    ),
    OrgPermission.ORG_PRIVACY.value: (
        "Only the organization's owners can change this."
    ),
    OrgPermission.RECRUITMENTS_SPEAK.value: (
        "Only the organization's owners can do this on a recruitment."
    ),
    OrgPermission.VERIFICATIONS_REVIEW.value: (
        "Only the organization's owners can review verifications."
    ),
    OrgPermission.ORG_BLOCK.value: (
        "Only the organization's owners can block accounts."
    ),
    OrgPermission.ORG_RECEIVE_ALERTS.value: (
        "You don't receive this organization's alerts."
    ),
}


def validate_permission_overrides(value):
    """
    ``{"grant": [...], "revoke": [...]}``, every name from the catalog.

    The field validator on ``OrganizationMember.permission_overrides``. A typo
    must fail where it is written: an unknown name in ``grant`` would otherwise
    be a permission nobody ever checks and a screen that lies about it.
    """
    if not isinstance(value, dict):
        raise ValidationError("permission_overrides must be an object.")

    unknown_keys = set(value) - {"grant", "revoke"}
    if unknown_keys:
        raise ValidationError(
            f"Unknown permission_overrides keys: {sorted(unknown_keys)}"
        )

    for key in ("grant", "revoke"):
        names = value.get(key, [])
        if not isinstance(names, list):
            raise ValidationError(f"permission_overrides.{key} must be a list.")

        unknown = [name for name in names if name not in OrgPermission.values]
        if unknown:
            raise ValidationError(f"Unknown permissions: {unknown}")


def member_permissions(member) -> frozenset:
    """
    What this member may do: role defaults ∪ grant − revoke.

    Nothing at all for a member who is not ACTIVE, which is what makes a stale
    row (an Actor built just before a removal) safe to hand in here.

    An owner's ``members.manage`` survives a revoke: the last-owner rules stand
    on owners being able to manage, and an org nobody can administer has no way
    back.
    """
    if member is None or member.status != "active":
        return frozenset()

    overrides = member.permission_overrides or {}
    granted = ROLE_PERMISSIONS.get(member.role, frozenset()) | set(
        overrides.get("grant") or ()
    )
    permissions = granted - set(overrides.get("revoke") or ())

    if member.role == OWNER_ROLE:
        permissions |= {OrgPermission.MEMBERS_MANAGE.value}

    return frozenset(permissions)


def has_org_permission(member, perm) -> bool:
    return str(perm) in member_permissions(member)


def require_org_permission(actor, perm, message=None):
    """
    The acting OrganizationMember, or PermissionDenied (→ 403).

    ``message`` is for a gate with its own words; a caller without any gets the
    sentence from ``DENIED_MESSAGES``.
    """
    if actor is None or not actor.is_org or actor.organization is None:
        raise PermissionDenied(NOT_ORG_ACTOR_MESSAGE)

    member = actor.organization_member
    if not has_org_permission(member, perm):
        raise PermissionDenied(message or DENIED_MESSAGES[str(perm)])

    return member


def members_with_permission(organization, perm):
    """
    The ACTIVE members who hold ``perm`` — the queryset behind every audience.

    A queryset rather than a list so a caller can narrow it
    (``.filter(user__is_active=True)``) and it stays one query.

    ``permission_overrides__contains={"revoke": [...]}`` tests the whole column
    on purpose. The shorter ``permission_overrides__revoke__contains`` reads a
    key most rows do not have; that is SQL NULL, and ``NOT NULL`` inside the
    ``exclude`` below would silently drop every member without overrides.
    """
    from apps.organization.models import OrganizationMember

    perm = str(perm)
    default_roles = [
        role for role, permissions in ROLE_PERMISSIONS.items()
        if perm in permissions
    ]

    revoked = models.Q(permission_overrides__contains={"revoke": [perm]})
    if perm == OrgPermission.MEMBERS_MANAGE.value:
        revoked &= ~models.Q(role=OWNER_ROLE)

    return (
        OrganizationMember.objects
        .filter(
            models.Q(role__in=default_roles)
            | models.Q(permission_overrides__contains={"grant": [perm]}),
            organization=organization,
            status=OrganizationMember.Status.ACTIVE,
        )
        .exclude(revoked)
    )
