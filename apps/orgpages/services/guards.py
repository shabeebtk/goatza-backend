"""
The checks every page write starts with: who is acting, the page's lock, and
the acting member re-checked under it.

Permissions come only from apps/organization/permissions.py. Nothing here (or
anywhere in orgpages) compares ``member.role``.

THE WRITE SHAPE, in every service:

    organization, member = require_page_permission(actor)      # 403 first
    ... validate the payload ...
    with transaction.atomic():
        page = lock_page(organization)                        # the FIRST lock
        relock_actor(member)                                  # still allowed?
        ... write ...
        bust_public_page(organization.id)                     # after commit

One lock per org — the PageSettings row — so two editors of one page queue in
one place, and every count behind a limit is read after it.
"""

from django.db import IntegrityError, transaction
from rest_framework.exceptions import PermissionDenied

from apps.organization.models import OrganizationMember
from apps.organization.permissions import (
    OrgPermission,
    has_org_permission,
    require_org_permission,
)
from apps.orgpages.errors import PageError
from apps.orgpages.models import PageSettings

SWITCH_TO_ORG_MESSAGE = "Switch to your organization account to do this."
ACTOR_LOST_MESSAGE = "You can no longer edit this page."

MANAGE = OrgPermission.PAGE_MANAGE


def require_page_permission(actor, perm=MANAGE):
    """``(organization, acting OrganizationMember)``, or 403."""
    if actor is None or not actor.is_org or actor.organization is None:
        raise PageError("switch_to_org", SWITCH_TO_ORG_MESSAGE, status_code=403)

    member = require_org_permission(actor, perm)
    return actor.organization, member


def page_settings_for(organization):
    """
    The org's PageSettings, created on first use. For READS: no lock.
    ``get_or_create`` survives two first reads racing.
    """
    page, _ = PageSettings.objects.get_or_create(
        organization=organization,
        defaults={
            "enquiry_form_enabled": PageSettings.default_enquiry_form(
                organization.type
            ),
        },
    )
    return page


def lock_page(organization):
    """
    The org's PageSettings row, locked — the first lock of every page write.

    A missing row is created in a savepoint first. Two first writes racing:
    the loser's insert waits on the unique index, fails, and both then lock
    the one row that exists.
    """
    page = (
        PageSettings.objects.select_for_update()
        .filter(organization=organization)
        .first()
    )
    if page is not None:
        return page

    try:
        with transaction.atomic():
            PageSettings.objects.create(
                organization=organization,
                enquiry_form_enabled=PageSettings.default_enquiry_form(
                    organization.type
                ),
            )
    except IntegrityError:
        pass

    return PageSettings.objects.select_for_update().get(organization=organization)


def relock_actor(member, perm=MANAGE, message=ACTOR_LOST_MESSAGE):
    """
    The acting member as they are NOW, under a lock, or 403.

    An actor is as old as its request: ``require_org_permission`` read a member
    loaded before this transaction, and the request may have waited behind the
    one that removed or demoted them. Same reason as squads' relock_actor.
    """
    locked = (
        OrganizationMember.all_objects
        .select_for_update(of=("self",))
        .select_related("user")
        .filter(id=member.id)
        .first()
    )

    if (
        locked is None
        or locked.status != OrganizationMember.Status.ACTIVE
        or not has_org_permission(locked, perm)
    ):
        raise PermissionDenied(message)

    return locked
