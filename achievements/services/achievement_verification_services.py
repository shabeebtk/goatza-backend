"""
The organization side of achievement verification.

Kept apart from AchievementService on purpose: that service answers "what may
the owner do to their own profile", this one answers "what may an organization
say about someone else's claim". The two never share a caller — the owner's
write path is a user actor, this one is only ever reached as an organization
actor.

Verification is a one-way door only in the moment: an achievement sits at
``pending`` until the credited org decides, and after that the OWNER can still
edit it freely — a material edit knocks it straight back to ``pending``
(AchievementService), which re-opens the request. Nothing here locks the row.

Failures raise DRF exceptions so the message reaches the client unchanged:

  * PermissionDenied (403) — not an org actor, wrong org, or a COACH/STAFF member
  * NotFound (404)         — no such achievement
  * ValidationError (400)  — the achievement is not pending a decision
"""

import logging

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import (
    NotFound,
    PermissionDenied,
    ValidationError,
)

from achievements.models import Achievement
from achievements.selectors.achievement_selectors import (
    decided_verification_requests_for,
    pending_verification_requests_for,
)
from notifications.services.notification_service import NotificationService
from organization.models import OrganizationMember
from utils.validations import is_valid_uuid

logger = logging.getLogger(__name__)


class AchievementVerificationService:

    # Confirming someone's award is a claim the organization is publicly
    # standing behind, so it sits with the roles that speak for the org. COACH
    # and STAFF can be members of many orgs and are not the org's voice.
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
                "Switch to your organization account to review achievements."
            )

        member = actor.organization_member

        if member is None or member.role not in AchievementVerificationService.REVIEWER_ROLES:
            raise PermissionDenied(
                "Only organization owners and admins can verify achievements."
            )

        return actor.organization, member

    @staticmethod
    def _get_reviewable_achievement(organization, achievement_id, target_status) -> Achievement:
        """
        Load one achievement, prove it credits ``organization``, and prove the
        decision being asked for is a real change.

        An org may revisit itself: a rejected claim can later be verified, and a
        verified one can be withdrawn, because organizations learn things after
        the fact. What is refused is a no-op — asking to verify something already
        verified, which is almost always a double-submit rather than an
        intention.

        The "is it ours" check comes before the status check so an org poking at
        another org's row learns nothing about its state.
        """
        if not is_valid_uuid(achievement_id):
            raise ValidationError(
                f"'{achievement_id}' is not a valid achievement id."
            )

        # The decision views serialize the result with
        # AchievementVerificationRequestSerializer, which reaches for the
        # claimant's profile and the sport — joined here so a verify costs one
        # query, not three.
        achievement = (
            Achievement.objects
            .select_related(
                "user",
                "user__profile",
                "awarded_by",
                "sport",
            )
            .filter(id=achievement_id)
            .first()
        )

        if achievement is None:
            raise NotFound("Achievement not found.")

        if achievement.awarded_by_id != organization.id:
            raise PermissionDenied(
                "This achievement does not credit your organization."
            )

        if achievement.verification_status == target_status:
            raise ValidationError(
                "This achievement is already "
                f"{achievement.get_verification_status_display().lower()}."
            )

        return achievement

    # =================================================================
    # QUEUES
    # =================================================================

    @staticmethod
    def list_pending_for_org(actor):
        """
        The acting org's work queue — every achievement crediting them that is
        still waiting on a decision, oldest first.

        Gated by the same reviewer rule the decisions use: a COACH cannot read
        the queue they cannot act on.
        """
        organization, _ = AchievementVerificationService.require_reviewer(actor)

        return pending_verification_requests_for(organization)

    @staticmethod
    def list_decided_for_org(actor):
        """
        The calls this org has already made, most recently touched first. A
        first-class list rather than a write-only log, because every row here is
        still revisitable.
        """
        organization, _ = AchievementVerificationService.require_reviewer(actor)

        return decided_verification_requests_for(organization)

    # =================================================================
    # DECISIONS
    # =================================================================

    @staticmethod
    def verify(actor, achievement_id) -> Achievement:
        """
        Confirm the claim. Stamps the deciding member's user on ``verified_by``
        — the person, not the org, so the audit trail survives them leaving.

        Also the way an org reverses an earlier rejection: any state except
        already-verified is a valid starting point.
        """
        organization, member = AchievementVerificationService.require_reviewer(actor)
        achievement = AchievementVerificationService._get_reviewable_achievement(
            organization, achievement_id, Achievement.VerificationStatus.VERIFIED
        )

        achievement.verification_status = Achievement.VerificationStatus.VERIFIED
        achievement.verified_by = member.user
        achievement.verified_at = timezone.now()
        achievement.save(
            update_fields=[
                "verification_status",
                "verified_by",
                "verified_at",
                "updated_at",
            ]
        )

        AchievementVerificationService._notify_decision(
            organization,
            achievement,
            NotificationService.achievement_verified,
        )

        return achievement

    @staticmethod
    def reject(actor, achievement_id, reason="") -> Achievement:
        """
        Decline the claim.

        The achievement is NOT deleted and the owner is not blocked — a rejected
        award stays on their profile carrying its status, and editing it puts it
        back in this queue. ``reason`` is the org's optional short note; it rides
        on the notification only, so the owner is told why without the award
        itself carrying a permanent public mark.

        ``verified_by`` stays null: nobody verified anything — and clearing it is
        what makes this safe to run on a row that WAS verified, which is how an
        org withdraws a confirmation it now doubts.
        """
        organization, member = AchievementVerificationService.require_reviewer(actor)
        achievement = AchievementVerificationService._get_reviewable_achievement(
            organization, achievement_id, Achievement.VerificationStatus.REJECTED
        )

        reason = AchievementVerificationService._clean_reason(reason)

        achievement.verification_status = Achievement.VerificationStatus.REJECTED
        achievement.verified_by = None
        achievement.verified_at = None
        achievement.save(
            update_fields=[
                "verification_status",
                "verified_by",
                "verified_at",
                "updated_at",
            ]
        )

        AchievementVerificationService._notify_decision(
            organization,
            achievement,
            NotificationService.achievement_rejected,
            reason=reason,
        )

        return achievement

    # =================================================================
    # HELPERS
    # =================================================================

    MAX_REASON_LENGTH = 200

    @staticmethod
    def _clean_reason(value) -> str:
        """
        A short note, not an essay — it has to fit a push notification body.
        Absent is fine; over-long is rejected rather than silently truncated,
        so the org sees exactly what the owner will read.
        """
        reason = (value or "").strip()

        if len(reason) > AchievementVerificationService.MAX_REASON_LENGTH:
            raise ValidationError(
                f"The reason cannot be longer than "
                f"{AchievementVerificationService.MAX_REASON_LENGTH} characters."
            )

        return reason

    @staticmethod
    def _notify_decision(organization, achievement, notify, **kwargs) -> None:
        """
        Tell the owner what the org decided.

        Deferred to after commit and never allowed to raise: a notification or
        FCM failure must not roll back a decision the org has already made.
        Same contract CareerVerificationService._notify_decision uses.
        """
        def _send():
            try:
                notify(actor_org=organization, achievement=achievement, **kwargs)
            except Exception as exc:
                logger.warning(
                    "AchievementVerificationService | decision notification "
                    f"failed | achievement_id={achievement.id} | {exc}"
                )

        transaction.on_commit(_send)
