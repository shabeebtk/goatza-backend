"""
Who is in an organization, how they got there, and how they stop being in it.

Every write is here, in one transaction, for two reasons worth keeping in mind:

THE TWO INVARIANTS ARE COUNTS. "Never zero owners" and "at most 50 members" are
both a read followed by a write, so each of those paths locks the org's member
rows first (``_lock_members``). The counts themselves are separate statements
AFTER the lock: under READ COMMITTED a waiter re-reads the rows it locked but
never sees a row inserted while it waited, and only a fresh statement does.
Locks are always taken members first, then the invite, so two paths cannot
deadlock on each other.

SIDE EFFECTS HAPPEN AFTER COMMIT. A notification, an email or a socket event
for something that rolled back would tell somebody a lie, so all of them run in
``transaction.on_commit(..., robust=True)`` — robust because the membership has
already changed by then, and a failed push must not answer 500 for it.

THE INVITE TOKEN IS A CREDENTIAL, like a guardian consent token: generated here,
hashed, the hash stored, the raw value handed to one email and never logged.
Log the invite id. Every failure to resolve one is the same 404, and an address
typed by an inviter is treated the same whether or not it has an account.
"""

import hashlib
import logging
import secrets
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.db.models import Case, IntegerField, Q, When
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied

from apps.accounts.models import User
from apps.moderation.services.block_guard import is_blocked
from apps.notifications.services.notification_service import NotificationService
from apps.organization.models import OrganizationInvite, OrganizationMember
from apps.organization.permissions import (
    OrgPermission,
    members_with_permission,
    require_org_permission,
)
from apps.accounts.services.account_deletion_service import mask_email
from utils.realtime import org_actor_sockets_group, safe_group_send
from utils.transactional_emails import send_org_invite_email
from utils.validations import is_valid_email, is_valid_uuid

logger = logging.getLogger(__name__)

MAX_ACTIVE_MEMBERS = 50
MAX_PENDING_INVITES = 20

# 32 bytes of urandom, base64url: the same size as a guardian consent token.
INVITE_TOKEN_BYTES = 32

# Self, an inactive account and a block all read the same, so a block can never
# be told apart from the other reasons.
CANNOT_INVITE_MESSAGE = "Couldn't invite this person."
INVITE_GONE_MESSAGE = "This invite isn't available any more."
INVITE_CLOSED_MESSAGE = "This invite is no longer available."
MEMBER_NOT_FOUND_MESSAGE = "This member wasn't found."
SWITCH_TO_PERSONAL_MESSAGE = "Switch to your personal account to do this."


class OrgMemberError(Exception):
    """A refusal the client can act on: a stable code and a human sentence."""

    def __init__(self, code, message, status_code=400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def _hash_token(raw_token) -> str:
    return hashlib.sha256((raw_token or "").encode()).hexdigest()


def _invite_address(invite) -> str:
    """Where this invite's link was mailed. Blank for an account with no email."""
    if invite.email:
        return invite.email
    return (invite.invited_user.email or "") if invite.invited_user else ""


def _person_name(user) -> str:
    profile = getattr(user, "profile", None)
    return getattr(profile, "name", "") or user.username or ""


def _org_is_usable(organization) -> bool:
    return organization.is_active and not organization.is_suspended


def _invite_gone():
    """The ONE answer for a token that does not resolve: no oracle."""
    return OrgMemberError("not_found", INVITE_GONE_MESSAGE, status_code=404)


def _invite_closed():
    return OrgMemberError("invite_not_pending", INVITE_CLOSED_MESSAGE)


def _require_valid_role(role):
    if role not in OrganizationMember.Role.values:
        raise OrgMemberError("invalid_role", "Choose a valid role.")


def _lock_members(organization):
    """Every writer that reads an owner count or a cap queues behind this."""
    return list(
        OrganizationMember.objects
        .select_for_update()
        .filter(organization=organization)
        .order_by("id")
    )


def _has_another_owner(organization, member) -> bool:
    """
    An owner besides ``member`` who could actually run the org. A deactivated
    account does not count: leaving it as the only owner orphans the org the
    moment that account is purged.
    """
    return (
        OrganizationMember.objects
        .filter(
            organization=organization,
            role=OrganizationMember.Role.OWNER,
            status=OrganizationMember.Status.ACTIVE,
            user__is_active=True,
        )
        .exclude(id=member.id)
        .exists()
    )


def _announce_membership(user_id, organization_id, status, role, *, close=False):
    """
    Tell one person's devices their membership changed; with ``close``, also end
    their sockets that act as the org.

    Two sends because a device is in exactly one of two places. A socket acting
    as the person sits in ``user_notifications_<user>``. A socket acting AS THE
    ORG sits in ``user_notifications_<org>``, shared with everyone else in the
    org, so it hears about this through the consumers' ``org_member_changed``
    handler, which forwards the frame only to the person it is about.
    """
    event = {
        "type": "notification_message",
        "notification_type": "org_membership_changed",
        "organization_id": str(organization_id),
        "status": status,
        "role": role,
    }
    safe_group_send(
        f"user_notifications_{user_id}",
        event,
        tag="organization.membership_changed",
    )
    safe_group_send(
        org_actor_sockets_group(organization_id),
        {
            **event,
            "type": "org_member_changed",
            "user_id": str(user_id),
            "close": close,
        },
        tag="organization.org_member_changed",
    )


def _notify_remaining_owners(organization, actor_member, departed_member):
    """
    A safeguard against one owner quietly pushing out another: the OTHER owners
    are told, with a push. The owner who did it already knows.
    """
    owners = (
        OrganizationMember.objects
        .filter(
            organization=organization,
            role=OrganizationMember.Role.OWNER,
            status=OrganizationMember.Status.ACTIVE,
        )
        .exclude(user_id__in=[actor_member.user_id, departed_member.user_id])
        .select_related("user")
    )
    for owner in owners:
        NotificationService.org_owner_changed(
            actor_user=actor_member.user,
            organization=organization,
            recipient_user=owner.user,
            removed_name=_person_name(departed_member.user),
        )


class OrganizationMemberService:

    @staticmethod
    def is_organization_member(org, user):
        return OrganizationMember.objects.filter(
            organization=org,
            user=user,
            status=OrganizationMember.Status.ACTIVE,
        ).exists()

    # -----------------------------------------------------------------
    # READS
    # -----------------------------------------------------------------
    @staticmethod
    def list_members(actor):
        """Owners first, then by when they joined."""
        require_org_permission(actor, OrgPermission.MEMBERS_VIEW)

        return (
            OrganizationMember.objects
            .filter(organization=actor.organization)
            .select_related(
                "user",
                "user__profile",
                "added_by__user",
                "added_by__user__profile",
            )
            .annotate(
                owner_rank=Case(
                    When(role=OrganizationMember.Role.OWNER, then=0),
                    default=1,
                    output_field=IntegerField(),
                )
            )
            .order_by("owner_rank", "joined_at")
        )

    @staticmethod
    def list_pending_invites(actor):
        require_org_permission(actor, OrgPermission.MEMBERS_MANAGE)

        return (
            OrganizationInvite.objects
            .filter(OrganizationInvite.open_q(), organization=actor.organization)
            .select_related(
                "invited_user",
                "invited_user__profile",
                "invited_by__user",
                "invited_by__user__profile",
            )
            .order_by("-created_at")
        )

    @staticmethod
    def my_invites(user):
        """This person's open invites, from organizations that can still be joined."""
        return (
            OrganizationInvite.objects
            .filter(
                OrganizationInvite.open_q(),
                invited_user=user,
                organization__is_active=True,
                organization__is_suspended=False,
            )
            .select_related(
                "organization",
                "organization__profile",
                "invited_by__user",
                "invited_by__user__profile",
            )
            .order_by("-created_at")
        )

    @staticmethod
    def invite_page(raw_token):
        """The public invite page's payload, or the one 404."""
        invite = OrganizationMemberService._open_invite_by_token(raw_token)
        organization = invite.organization
        profile = getattr(organization, "profile", None)

        return {
            "organization": {
                "name": organization.name,
                "username": organization.username,
                "logo": getattr(profile, "logo", "") or "",
            },
            "role": invite.role,
            "invited_by_name": (
                _person_name(invite.invited_by.user) if invite.invited_by else ""
            ),
            "email_masked": mask_email(_invite_address(invite)),
            "status": OrganizationInvite.Status.PENDING,
        }

    # -----------------------------------------------------------------
    # SENDING AN INVITE
    # -----------------------------------------------------------------
    @staticmethod
    def invite(actor, *, user_id=None, email=None, role):
        inviter = require_org_permission(actor, OrgPermission.MEMBERS_MANAGE)
        organization = actor.organization

        _require_valid_role(role)

        email = (email or "").strip().lower()
        if bool(user_id) == bool(email):
            raise OrgMemberError(
                "invalid_target", "Choose a person or enter an email address."
            )
        if email and not is_valid_email(email):
            raise OrgMemberError("invalid_email", "Enter a valid email address.")

        target, deliver = OrganizationMemberService._resolve_invite_target(
            organization, inviter, user_id, email
        )

        raw_token = secrets.token_urlsafe(INVITE_TOKEN_BYTES)

        try:
            with transaction.atomic():
                _lock_members(organization)

                OrganizationMemberService._refuse_duplicate_invite(
                    organization, target, email
                )

                if (
                    OrganizationMember.objects
                    .filter(organization=organization)
                    .count() >= MAX_ACTIVE_MEMBERS
                ):
                    raise OrgMemberError(
                        "member_limit",
                        f"An organization can have up to {MAX_ACTIVE_MEMBERS} "
                        f"members.",
                    )

                if (
                    OrganizationInvite.objects
                    .filter(OrganizationInvite.open_q(), organization=organization)
                    .count() >= MAX_PENDING_INVITES
                ):
                    raise OrgMemberError(
                        "invite_limit",
                        f"You can have up to {MAX_PENDING_INVITES} pending "
                        f"invites. Cancel one to send another.",
                    )

                invite = OrganizationInvite.objects.create(
                    organization=organization,
                    role=role,
                    invited_by=inviter,
                    invited_user=target,
                    email=email,
                    token_hash=_hash_token(raw_token),
                    expires_at=(
                        timezone.now()
                        + timedelta(days=OrganizationInvite.TTL_DAYS)
                    ),
                )

                if deliver:
                    transaction.on_commit(
                        lambda: OrganizationMemberService._deliver_invite(
                            invite, raw_token
                        ),
                        robust=True,
                    )
        except IntegrityError:
            # A signup claiming an email invite can land between the check and
            # the insert. The constraint had the final say, and this is it.
            raise OrgMemberError(
                "already_invited", "This person already has a pending invite."
            )

        logger.info(
            "[ORG INVITE] sent | invite=%s | org=%s", invite.id, organization.id
        )

        return invite

    @staticmethod
    def _resolve_invite_target(organization, inviter, user_id, email):
        """
        ``(account the invite attaches to or None, whether to deliver it)``.

        An invite BY ACCOUNT names somebody the inviter can already see, so a
        refusal gives nothing away. An invite BY EMAIL must answer the same
        whether or not the address has an account:

          * An account that cannot answer in the app (unverified, deactivated)
            is just an address: the link goes there, and
            ``claim_email_invites`` attaches it if the address is ever verified.
          * An account on the other side of a block gets NOTHING, and the
            inviter still sees a normal pending invite that simply expires. A
            refusal here would confirm the account exists; a delivery would
            contact somebody who asked not to hear from this org.
        """
        if email:
            account = User.objects.filter(email__iexact=email).first()

            if account is None:
                return None, True

            if account.id == inviter.user_id:
                raise OrgMemberError("cannot_invite", CANNOT_INVITE_MESSAGE)

            if not (account.is_active and account.is_email_verified):
                return None, True

            if is_blocked(account, organization):
                return None, False

            return account, True

        account = (
            User.objects.filter(id=user_id).first()
            if is_valid_uuid(user_id) else None
        )

        if (
            account is None
            or account.id == inviter.user_id
            or not account.is_active
            or account.deletion_requested_at is not None
            or is_blocked(account, organization)
        ):
            raise OrgMemberError("cannot_invite", CANNOT_INVITE_MESSAGE)

        return account, True

    @staticmethod
    def _refuse_duplicate_invite(organization, target, email):
        if (
            target is not None
            and OrganizationMember.objects
            .filter(organization=organization, user=target)
            .exists()
        ):
            raise OrgMemberError(
                "already_member", "This person is already a member."
            )

        same_person = Q()
        if target is not None:
            same_person |= Q(invited_user=target)
        if email:
            same_person |= Q(email=email)

        pending = OrganizationInvite.objects.filter(
            same_person,
            organization=organization,
            status=OrganizationInvite.Status.PENDING,
        )

        # A pending row past its date IS expired, but the unique constraints
        # only know its status. Stamp it now or it blocks a fresh invite.
        now = timezone.now()
        pending.filter(expires_at__lte=now).update(
            status=OrganizationInvite.Status.EXPIRED
        )

        if pending.exists():
            raise OrgMemberError(
                "already_invited", "This person already has a pending invite."
            )

    @staticmethod
    def _deliver_invite(invite, raw_token):
        if invite.invited_user_id:
            NotificationService.org_invite(invite, invite.invited_user)

        send_org_invite_email(invite, raw_token)

    @staticmethod
    def cancel_invite(actor, invite_id):
        require_org_permission(actor, OrgPermission.MEMBERS_MANAGE)

        with transaction.atomic():
            invite = (
                OrganizationInvite.objects
                .select_for_update()
                .filter(id=invite_id, organization=actor.organization)
                .first()
            )
            if invite is None:
                raise OrgMemberError(
                    "not_found", INVITE_GONE_MESSAGE, status_code=404
                )
            if not invite.is_open:
                raise OrgMemberError(
                    "invite_not_pending", "This invite is no longer pending."
                )

            invite.status = OrganizationInvite.Status.CANCELLED
            invite.responded_at = timezone.now()
            invite.save(update_fields=["status", "responded_at"])

        logger.info("[ORG INVITE] cancelled | invite=%s", invite.id)

    # -----------------------------------------------------------------
    # ANSWERING AN INVITE, BY ID (the invited person in the app)
    # -----------------------------------------------------------------
    @staticmethod
    def _own_invite(user, invite_id):
        invite = (
            OrganizationInvite.objects
            .filter(id=invite_id, invited_user=user)
            .first()
        )
        if invite is None:
            raise OrgMemberError(
                "not_found", INVITE_GONE_MESSAGE, status_code=404
            )
        return invite

    @staticmethod
    def accept_invite(user, invite_id):
        invite = OrganizationMemberService._own_invite(user, invite_id)
        return OrganizationMemberService._accept(invite, user, _invite_closed())

    @staticmethod
    def decline_invite(user, invite_id):
        invite = OrganizationMemberService._own_invite(user, invite_id)
        OrganizationMemberService._decline(invite, user, _invite_closed())

    # -----------------------------------------------------------------
    # ANSWERING AN INVITE, BY TOKEN (the email link)
    # -----------------------------------------------------------------
    @staticmethod
    def _open_invite_by_token(raw_token):
        invite = (
            OrganizationInvite.objects
            .filter(token_hash=_hash_token(raw_token))
            .select_related(
                "organization",
                "organization__profile",
                "invited_user",
                "invited_by__user",
                "invited_by__user__profile",
            )
            .first()
        )

        if (
            invite is None
            or not invite.is_open
            or not _org_is_usable(invite.organization)
        ):
            raise _invite_gone()

        return invite

    @staticmethod
    def accept_invite_by_token(user, raw_token):
        """
        The address has to be proved, not just the link: a forwarded invite must
        not make the wrong person a member. Holding the link is enough to DECLINE
        below, since the worst that does is send the inviter back to try again.
        """
        invite = OrganizationMemberService._open_invite_by_token(raw_token)

        account_email = (user.email or "").lower()
        may_accept = invite.invited_user_id == user.id or (
            invite.email
            and user.is_email_verified
            and account_email == invite.email
        )
        if not may_accept:
            raise OrgMemberError(
                "email_mismatch",
                f"This invite was sent to {mask_email(_invite_address(invite))}.",
                status_code=403,
            )

        return OrganizationMemberService._accept(invite, user, _invite_gone())

    @staticmethod
    def decline_invite_by_token(user, raw_token):
        invite = OrganizationMemberService._open_invite_by_token(raw_token)
        OrganizationMemberService._decline(invite, user, _invite_gone())

    # -----------------------------------------------------------------
    # THE TWO ANSWERS (one path each, whichever door they came through)
    # -----------------------------------------------------------------
    @staticmethod
    def _accept(invite, user, unavailable):
        """Returns ``(organization, member)``. ``unavailable`` is what to raise."""
        organization = invite.organization

        with transaction.atomic():
            _lock_members(organization)

            invite = (
                OrganizationInvite.objects
                .select_for_update()
                .get(id=invite.id)
            )
            if (
                not invite.is_open
                or not _org_is_usable(organization)
                or is_blocked(user, organization)
            ):
                raise unavailable

            member = (
                OrganizationMember.all_objects
                .select_for_update()
                .filter(organization=organization, user=user)
                .first()
            )
            if member is not None and not member.is_former:
                raise OrgMemberError(
                    "already_member", "You're already a member of this organization."
                )

            if (
                OrganizationMember.objects
                .filter(organization=organization)
                .count() >= MAX_ACTIVE_MEMBERS
            ):
                raise OrgMemberError(
                    "member_limit",
                    f"{organization.name} has reached its limit of "
                    f"{MAX_ACTIVE_MEMBERS} members.",
                )

            now = timezone.now()

            if member is None:
                member = OrganizationMember.objects.create(
                    organization=organization,
                    user=user,
                    role=invite.role,
                    joined_at=now,
                    added_by=invite.invited_by,
                )
            else:
                # A returning person reactivates their row, so their history
                # stays theirs. Overrides from last time do not come back: what
                # they may do now is what this invite said.
                member.role = invite.role
                member.status = OrganizationMember.Status.ACTIVE
                member.joined_at = now
                member.added_by = invite.invited_by
                member.removed_at = None
                member.removed_by = None
                member.permission_overrides = {}
                member.save(
                    update_fields=[
                        "role", "status", "joined_at", "added_by", "removed_at",
                        "removed_by", "permission_overrides",
                    ]
                )

            invite.status = OrganizationInvite.Status.ACCEPTED
            invite.responded_at = now
            invite.accepted_member = member
            invite.invited_user = user
            invite.save(
                update_fields=[
                    "status", "responded_at", "accepted_member", "invited_user",
                ]
            )

            # They could also hold a second invite under their address. Now it
            # is true of nobody, so the pending list should stop saying so.
            others = Q(invited_user=user)
            if user.email:
                others |= Q(email=user.email.lower())
            OrganizationInvite.objects.filter(
                others,
                organization=organization,
                status=OrganizationInvite.Status.PENDING,
            ).exclude(id=invite.id).update(
                status=OrganizationInvite.Status.CANCELLED, responded_at=now
            )

            transaction.on_commit(
                lambda: OrganizationMemberService._after_accept(
                    organization, user, member
                ),
                robust=True,
            )

        logger.info(
            "[ORG INVITE] accepted | invite=%s | org=%s | user=%s",
            invite.id, organization.id, user.id,
        )

        return organization, member

    @staticmethod
    def _after_accept(organization, user, member):
        managers = (
            members_with_permission(organization, OrgPermission.MEMBERS_MANAGE)
            .exclude(user=user)
            .select_related("user")
        )
        for manager in managers:
            NotificationService.org_invite_accepted(
                actor_user=user,
                organization=organization,
                recipient_user=manager.user,
                role=member.role,
            )

        _announce_membership(
            user.id,
            organization.id,
            OrganizationMember.Status.ACTIVE.value,
            member.role,
        )

    @staticmethod
    def _decline(invite, user, unavailable):
        organization = invite.organization

        with transaction.atomic():
            invite = (
                OrganizationInvite.objects
                .select_for_update()
                .get(id=invite.id)
            )
            if not invite.is_open or not _org_is_usable(organization):
                raise unavailable

            invite.status = OrganizationInvite.Status.DECLINED
            invite.responded_at = timezone.now()
            invite.save(update_fields=["status", "responded_at"])

            transaction.on_commit(
                lambda: OrganizationMemberService._after_decline(
                    invite, organization, user
                ),
                robust=True,
            )

        logger.info("[ORG INVITE] declined | invite=%s", invite.id)

    @staticmethod
    def _after_decline(invite, organization, user):
        """
        To the inviter only, in-app only. An invite made by email keeps its
        masked address here, because naming the account that declined would
        answer the question the pending list is careful not to.
        """
        inviter = invite.invited_by
        if inviter is None or inviter.is_former:
            return

        NotificationService.org_invite_declined(
            organization=organization,
            recipient_user=inviter.user,
            member_name=(
                mask_email(invite.email) if invite.email else _person_name(user)
            ),
        )

    # -----------------------------------------------------------------
    # AN EMAIL BECAME VERIFIED
    # -----------------------------------------------------------------
    @staticmethod
    def claim_email_invites(user):
        """
        Attach the open invites sent to this address before it had a verified
        account, and tell the person. Called wherever an address is verified:
        OTP signup, Google login and a confirmed email change.

        NEVER RAISES. It runs inside login and signup, and an invite must not
        cost somebody their session. Returns how many were claimed.
        """
        try:
            return OrganizationMemberService._claim_email_invites(user)
        except Exception:
            logger.exception("[ORG INVITE] claim failed | user=%s", user.id)
            return 0

    @staticmethod
    def _claim_email_invites(user):
        email = (user.email or "").strip().lower()
        if not email or not user.is_email_verified:
            return 0

        invites = (
            OrganizationInvite.objects
            .filter(
                OrganizationInvite.open_q(),
                email=email,
                invited_user__isnull=True,
            )
            .select_related(
                "organization",
                "organization__profile",
                "invited_by__user",
                "invited_by__user__profile",
            )
        )

        claimed = 0
        for invite in invites:
            organization = invite.organization

            if (
                not _org_is_usable(organization)
                or is_blocked(user, organization)
                or OrganizationMember.objects
                .filter(organization=organization, user=user)
                .exists()
            ):
                continue

            try:
                with transaction.atomic():
                    updated = (
                        OrganizationInvite.objects
                        .filter(id=invite.id, invited_user__isnull=True)
                        .update(invited_user=user)
                    )
            except IntegrityError:
                # They already hold a pending invite from this org by account.
                continue

            if updated:
                invite.invited_user = user
                NotificationService.org_invite(invite, user)
                claimed += 1

        if claimed:
            logger.info(
                "[ORG INVITE] claimed | user=%s | count=%s", user.id, claimed
            )

        return claimed

    # -----------------------------------------------------------------
    # CHANGING A MEMBER
    # -----------------------------------------------------------------
    @staticmethod
    def _locked_member(locked_members, member_id):
        for member in locked_members:
            if str(member.id) == str(member_id):
                return member
        raise OrgMemberError(
            "not_found", MEMBER_NOT_FOUND_MESSAGE, status_code=404
        )

    @staticmethod
    def change_role(actor, member_id, role):
        manager = require_org_permission(actor, OrgPermission.MEMBERS_MANAGE)
        organization = actor.organization

        _require_valid_role(role)

        with transaction.atomic():
            member = OrganizationMemberService._locked_member(
                _lock_members(organization), member_id
            )

            previous_role = member.role
            if previous_role == role:
                return OrganizationMemberService._member_for_response(member.id)

            was_owner = previous_role == OrganizationMember.Role.OWNER
            if (
                was_owner
                and not _has_another_owner(organization, member)
            ):
                raise OrgMemberError(
                    "last_owner", f"{organization.name} needs at least one owner."
                )

            member.role = role
            member.save(update_fields=["role"])

            transaction.on_commit(
                lambda: OrganizationMemberService._after_role_change(
                    organization, manager, member, was_owner
                ),
                robust=True,
            )

        logger.info(
            "[ORG MEMBER] role changed | org=%s | member=%s | role=%s",
            organization.id, member.id, role,
        )

        return OrganizationMemberService._member_for_response(member.id)

    @staticmethod
    def _after_role_change(organization, manager, member, was_owner):
        if member.id != manager.id:
            NotificationService.org_role_changed(
                organization=organization,
                recipient_user=member.user,
                role=member.role,
            )

        if was_owner:
            _notify_remaining_owners(organization, manager, member)

        # No socket close: a socket carries no role, and nothing it may do has
        # changed. The frame is what makes the client's buttons update.
        _announce_membership(
            member.user_id, organization.id, "role_changed", member.role
        )

    @staticmethod
    def _member_for_response(member_id):
        return (
            OrganizationMember.objects
            .select_related(
                "user",
                "user__profile",
                "added_by__user",
                "added_by__user__profile",
            )
            .get(id=member_id)
        )

    @staticmethod
    def remove_member(actor, member_id):
        manager = require_org_permission(actor, OrgPermission.MEMBERS_MANAGE)
        organization = actor.organization

        with transaction.atomic():
            member = OrganizationMemberService._locked_member(
                _lock_members(organization), member_id
            )

            if member.id == manager.id:
                raise OrgMemberError(
                    "cannot_remove_self",
                    "You can't remove yourself. Leave the organization instead.",
                )

            was_owner = member.role == OrganizationMember.Role.OWNER
            if was_owner and not _has_another_owner(organization, member):
                raise OrgMemberError(
                    "last_owner", f"{organization.name} needs at least one owner."
                )

            member.status = OrganizationMember.Status.REMOVED
            member.removed_at = timezone.now()
            member.removed_by = manager
            member.save(update_fields=["status", "removed_at", "removed_by"])

            transaction.on_commit(
                lambda: OrganizationMemberService._after_remove(
                    organization, manager, member, was_owner
                ),
                robust=True,
            )

        logger.info(
            "[ORG MEMBER] removed | org=%s | member=%s", organization.id, member.id
        )

    @staticmethod
    def _after_remove(organization, manager, member, was_owner):
        NotificationService.org_member_removed(
            organization=organization, recipient_user=member.user
        )

        if was_owner:
            _notify_remaining_owners(organization, manager, member)

        _announce_membership(
            member.user_id,
            organization.id,
            OrganizationMember.Status.REMOVED.value,
            member.role,
            close=True,
        )

    @staticmethod
    def leave(actor):
        if actor is None or not actor.is_org or actor.organization_member is None:
            raise PermissionDenied(
                "Switch to your organization account to do this."
            )

        organization = actor.organization

        with transaction.atomic():
            member = OrganizationMemberService._locked_member(
                _lock_members(organization), actor.organization_member.id
            )

            if (
                member.role == OrganizationMember.Role.OWNER
                and not _has_another_owner(organization, member)
            ):
                raise OrgMemberError(
                    "last_owner", "Make someone else an owner first."
                )

            member.status = OrganizationMember.Status.LEFT
            member.removed_at = timezone.now()
            member.save(update_fields=["status", "removed_at"])

            transaction.on_commit(
                lambda: _announce_membership(
                    member.user_id,
                    organization.id,
                    OrganizationMember.Status.LEFT.value,
                    member.role,
                    close=True,
                ),
                robust=True,
            )

        logger.info(
            "[ORG MEMBER] left | org=%s | member=%s", organization.id, member.id
        )
