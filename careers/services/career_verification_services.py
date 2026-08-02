"""
The organization side of career verification.

Kept apart from CareerEntryService on purpose: that service answers "what may
the owner do to their own history", this one answers "what may a club say about
someone else's claim". The two never share a caller — the owner's write path is
a user actor, this one is only ever reached as an organization actor.

Verification is a one-way door only in the moment: an entry sits at ``pending``
until a club decides, and after that the OWNER can still edit it freely — a
material edit knocks it straight back to ``pending`` (CareerEntryService), which
re-opens the request. Nothing here locks the entry.

Failures raise DRF exceptions so the message reaches the client unchanged:

  * PermissionDenied (403) — not an org actor, wrong org, or a COACH/STAFF member
  * NotFound (404)         — no such entry
  * ValidationError (400)  — the entry is not pending a decision
"""

import logging

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import (
    NotFound,
    PermissionDenied,
    ValidationError,
)

from careers.models import CareerEntry
from notifications.services.notification_service import NotificationService
from organization.models import OrganizationMember
from utils.validations import is_valid_uuid

logger = logging.getLogger(__name__)


class CareerVerificationService:

    # Confirming someone's career is a claim the club is publicly standing
    # behind, so it sits with the roles that speak for the org. COACH and STAFF
    # can be members of many clubs and are not the org's voice.
    #
    # Deliberately the same pair `notifications._get_recipient_users` pushes an
    # org notification to: exactly the people who are told about a request are
    # the people who can act on it.
    REVIEWER_ROLES = (
        OrganizationMember.Role.OWNER,
        OrganizationMember.Role.ADMIN,
    )

    # =================================================================
    # GUARDS
    # =================================================================

    @staticmethod
    def require_reviewer(actor):
        """
        The single gate for the org side. Returns
        ``(organization, organization_member)``, or raises PermissionDenied
        (→ 403).

        ``resolve_actor`` has already proved the logged-in user is a member of
        the org they claim to act as; this only adds the role rule on top.

        Public because the write views call it *before* validating the body: a
        COACH should get 403, not a critique of a payload that was never going
        to be accepted. Every write here calls it again anyway, so the rule
        still lives in exactly one place.
        """
        if actor is None or not actor.is_org or actor.organization is None:
            raise PermissionDenied(
                "Switch to your organization account to review career entries."
            )

        member = actor.organization_member

        if member is None or member.role not in CareerVerificationService.REVIEWER_ROLES:
            raise PermissionDenied(
                "Only organization owners and admins can verify career entries."
            )

        return actor.organization, member

    @staticmethod
    def _get_reviewable_entry(organization, entry_id, target_status) -> CareerEntry:
        """
        Load one entry, prove it names ``organization``, and prove the decision
        being asked for is a real change.

        A club may revisit itself: a rejected claim can later be verified, and a
        verified one can be withdrawn, because clubs learn things after the
        fact. What is refused is a no-op — asking to verify something already
        verified, which is almost always a double-submit rather than an
        intention.

        The "is it ours" check comes before the status check so a club poking at
        another club's entry learns nothing about its state.
        """
        if not is_valid_uuid(entry_id):
            raise ValidationError(f"'{entry_id}' is not a valid career entry id.")

        # The decision views serialize the result with CareerEntrySerializer,
        # which reaches for the org's profile (logo), the sport and the
        # positions — joined here so a verify costs one query, not four.
        entry = (
            CareerEntry.objects
            .select_related(
                "user",
                "organization",
                "organization__profile",
                "sport",
            )
            .prefetch_related("positions")
            .filter(id=entry_id)
            .first()
        )

        if entry is None:
            raise NotFound("Career entry not found.")

        if entry.organization_id != organization.id:
            raise PermissionDenied(
                "This career entry does not name your club."
            )

        if entry.verification_status == target_status:
            raise ValidationError(
                "This career entry is already "
                f"{entry.get_verification_status_display().lower()}."
            )

        return entry

    # =================================================================
    # DECISIONS
    # =================================================================

    @staticmethod
    def verify_entry(actor, entry_id) -> CareerEntry:
        """
        Confirm the claim. Stamps the deciding member's user on
        ``verified_by`` — the person, not the org, so the audit trail survives
        them leaving the club.

        Also the way a club reverses an earlier rejection: any state except
        already-verified is a valid starting point.
        """
        organization, member = CareerVerificationService.require_reviewer(actor)
        entry = CareerVerificationService._get_reviewable_entry(
            organization, entry_id, CareerEntry.VerificationStatus.VERIFIED
        )

        entry.verification_status = CareerEntry.VerificationStatus.VERIFIED
        entry.verified_by = member.user
        entry.verified_at = timezone.now()
        entry.save(
            update_fields=[
                "verification_status",
                "verified_by",
                "verified_at",
                "updated_at",
            ]
        )

        CareerVerificationService._notify_decision(
            organization,
            entry,
            NotificationService.career_verified,
        )

        return entry

    @staticmethod
    def reject_entry(actor, entry_id, reason="") -> CareerEntry:
        """
        Decline the claim.

        The entry is NOT deleted and the player is not blocked — a rejected
        entry stays on their profile carrying its status, and editing it puts
        it back in this queue. ``reason`` is the club's optional short note; it
        rides on the notification only, so the player is told why without the
        entry itself carrying a permanent public mark.

        ``verified_by`` stays null: nobody verified anything — and clearing it
        is what makes this safe to run on an entry that WAS verified, which is
        how a club withdraws a confirmation it now doubts.
        """
        organization, member = CareerVerificationService.require_reviewer(actor)
        entry = CareerVerificationService._get_reviewable_entry(
            organization, entry_id, CareerEntry.VerificationStatus.REJECTED
        )

        reason = CareerVerificationService._clean_reason(reason)

        entry.verification_status = CareerEntry.VerificationStatus.REJECTED
        entry.verified_by = None
        entry.verified_at = None
        entry.save(
            update_fields=[
                "verification_status",
                "verified_by",
                "verified_at",
                "updated_at",
            ]
        )

        CareerVerificationService._notify_decision(
            organization,
            entry,
            NotificationService.career_rejected,
            reason=reason,
        )

        return entry

    # =================================================================
    # HELPERS
    # =================================================================

    MAX_REASON_LENGTH = 200

    @staticmethod
    def _clean_reason(value) -> str:
        """
        A short note, not an essay — it has to fit a push notification body.
        Absent is fine; over-long is rejected rather than silently truncated,
        so the club sees exactly what the player will read.
        """
        reason = (value or "").strip()

        if len(reason) > CareerVerificationService.MAX_REASON_LENGTH:
            raise ValidationError(
                f"The reason cannot be longer than "
                f"{CareerVerificationService.MAX_REASON_LENGTH} characters."
            )

        return reason

    @staticmethod
    def _notify_decision(organization, entry, notify, **kwargs) -> None:
        """
        Tell the player what the club decided.

        Deferred to after commit and never allowed to raise: a notification or
        FCM failure must not roll back a decision the club has already made.
        Same contract ApplicationService.change_status uses.
        """
        def _send():
            try:
                notify(actor_org=organization, career_entry=entry, **kwargs)
            except Exception as exc:
                logger.warning(
                    "CareerVerificationService | decision notification failed "
                    f"| entry_id={entry.id} | {exc}"
                )

        transaction.on_commit(_send)
