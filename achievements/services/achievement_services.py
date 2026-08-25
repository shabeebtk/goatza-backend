"""
Write path for achievements.

Views stay thin: they hand ``request.actor`` and the shaped payload straight in
and every rule is applied here — user actors only, own achievements only, the
two caps, the issuer/verification coupling, and the career-entry integrity
check.

The first argument is the resolved ``core.actor.Actor``, not a User. "Your own
achievements" is not just an ownership check, it also means *acting as
yourself*: a person acting through one of their organizations is not editing
their personal profile, and only the actor knows which it was.

Where this deliberately differs from CareerEntryService:

  * an achievement is a moment, not a span — one ``achieved_date``, and the only
    date rule is that you cannot have won something tomorrow. That rule cannot
    be a DB CheckConstraint (no ``now()``), so it lives here alone.
  * an issuer is genuinely optional. A career entry always names a club, even if
    only as free text; plenty of achievements have nobody who issued them, so
    ``awarded_by_name`` may be empty and no "pick one or type one" rule applies.
  * pinning is capped, and the cap is enforced on the way in rather than by
    silently unpinning something else — which of your three pins to give up is
    your decision, not ours.

Failures raise DRF exceptions so the message reaches the client unchanged:

  * PermissionDenied (403) — acting as an organization, or not your achievement
  * NotFound (404)         — no such achievement
  * ValidationError (400)  — everything else (caps, bad dates, a career entry
    that is not yours or is for another sport, unknown organization or sport)
"""

import logging
import uuid

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import (
    NotFound,
    PermissionDenied,
    ValidationError,
)

from accounts.models import User
from achievements.models import Achievement
from careers.models import CareerEntry
from notifications.models import Notification
from notifications.services.notification_service import NotificationService
from organization.models import Organization
from sports.models import Sport
from utils.validations import is_valid_uuid

logger = logging.getLogger(__name__)


class AchievementService:

    # An achievements section is a trophy shelf, not a results archive. Twice the
    # career cap because awards accumulate faster than stints do — a decent youth
    # career produces one club spell and half a dozen medals — but still bounded,
    # so the profile stays scannable and one person cannot flood an org's
    # verification queue. Enforced here rather than in the serializer so every
    # write path shares it.
    MAX_ACHIEVEMENTS = 20

    # How many can sit above the fold. Three is what the profile card row holds;
    # past that "pinned" stops meaning anything.
    MAX_PINNED = 3

    # Editing any of these is a claim about the award itself, so a verified
    # achievement has to be re-checked.
    #
    # The four that are absent are absent on purpose. ``description`` and
    # ``reference_link`` are the owner's own commentary and sourcing — rewording
    # your blurb or fixing a dead news link does not invalidate a federation's
    # confirmation. ``career_entry`` is a cross-reference to elsewhere on the
    # profile, not part of what was claimed. And ``is_pinned`` is pure display:
    # knocking a verified award back to pending because its owner rearranged
    # their profile would be absurd, and it would let one careless tap flood a
    # club with re-review requests.
    MATERIAL_FIELDS = (
        "title",
        "achievement_type",
        "sport",
        "event_name",
        "level",
        "awarded_by",
        "awarded_by_name",
        "achieved_date",
        "image",
    )

    # =================================================================
    # GUARDS
    # =================================================================

    @staticmethod
    def require_user(actor) -> User:
        """
        The single write gate. Returns the owning user, or raises
        PermissionDenied (→ 403) when the actor may not manage achievements:
        signed out, or acting as an organization.

        Public because the write views call it *before* validating the body:
        someone who may not write at all should get 403, not a critique of a
        payload that was never going to be accepted. Every write here calls it
        again anyway, so the rule still lives in exactly one place.

        No role check — coaches and scouts win things too (coaching badges,
        manager of the season), same reasoning careers uses.
        """
        if actor is None or not actor.is_user or actor.user is None:
            if actor is not None and actor.is_org:
                raise PermissionDenied(
                    "Achievements belong to a person, not an organization. "
                    "Switch to your personal account to manage yours."
                )
            raise PermissionDenied(
                "You must be signed in to manage achievements."
            )

        return actor.user

    @staticmethod
    def _assert_under_cap(user) -> None:
        """
        Refuse a create once the user is at MAX_ACHIEVEMENTS.

        Not a race-proof lock — two simultaneous creates could both pass — but
        the cap is a product limit, not a correctness invariant, and one extra
        row is not worth serializing every write behind a row lock. Same
        reasoning as the career cap.
        """
        count = Achievement.objects.filter(user=user).count()

        if count >= AchievementService.MAX_ACHIEVEMENTS:
            raise ValidationError(
                f"You can have up to {AchievementService.MAX_ACHIEVEMENTS} "
                f"achievements. Delete one to add another."
            )

    @staticmethod
    def _assert_pin_allowed(user, *, exclude_id=None) -> None:
        """
        Refuse a pin once the user already has MAX_PINNED.

        Fails loudly instead of quietly unpinning the oldest: the whole point of
        a pin is that the owner chose it, so the client is told to unpin
        something rather than having that choice made for it.

        ``exclude_id`` leaves the achievement being edited out of the count, so
        re-sending ``is_pinned: true`` on something already pinned is a no-op
        rather than tripping its own cap.
        """
        pinned = Achievement.objects.filter(user=user, is_pinned=True)

        if exclude_id is not None:
            pinned = pinned.exclude(id=exclude_id)

        if pinned.count() >= AchievementService.MAX_PINNED:
            raise ValidationError(
                f"You can pin up to {AchievementService.MAX_PINNED} "
                f"achievements. Unpin one to pin this."
            )

    @staticmethod
    def _get_owned_achievement(user, achievement_id) -> Achievement:
        """
        Load one achievement and prove it belongs to ``user``. A missing row
        reads as gone (404); someone else's reads as forbidden (403).
        """
        if not is_valid_uuid(achievement_id):
            raise ValidationError(
                f"'{achievement_id}' is not a valid achievement id."
            )

        achievement = (
            Achievement.objects
            .filter(id=achievement_id)
            .first()
        )

        if achievement is None:
            raise NotFound("Achievement not found.")

        if achievement.user_id != user.id:
            raise PermissionDenied(
                "You can only manage your own achievements."
            )

        return achievement

    # =================================================================
    # FIELD RESOLUTION / CLEANING
    # =================================================================

    @staticmethod
    def _resolve_sport(sport_id) -> Sport:
        """An achievement is always about one sport, and it has to be a real one."""
        if not sport_id or not is_valid_uuid(sport_id):
            raise ValidationError("A valid sport is required.")

        sport = Sport.objects.filter(id=sport_id).first()

        if sport is None:
            raise ValidationError("That sport does not exist.")

        return sport

    @staticmethod
    def _resolve_organization(organization_id) -> Organization:
        """
        Resolve the body being credited with issuing the award. Only reached
        when the caller actually sent an id — an achievement with no issuer is
        normal (a federation that is not on Goatza, or nobody in particular) and
        is carried by ``awarded_by_name`` alone, or by nothing at all.
        """
        if not is_valid_uuid(organization_id):
            raise ValidationError(
                f"'{organization_id}' is not a valid organization id."
            )

        organization = (
            Organization.objects
            .filter(id=organization_id, is_active=True)
            .first()
        )

        if organization is None:
            raise ValidationError("That organization is no longer on Goatza.")

        return organization

    @staticmethod
    def _resolve_career_entry(user, sport, career_entry_id) -> CareerEntry:
        """
        Resolve the stint this award was won during, and prove it is coherent:

          * it has to be the requester's own entry — linking someone else's
            would let anyone hang their medals off a stranger's career, and
            reads as forbidden rather than missing
          * it has to be for the same sport — a basketball MVP award cannot
            belong to a football spell, the same integrity rule careers applies
            to positions

        The sport check is against the achievement's sport, not the entry's, so
        it holds identically whether the sport, the link, or both are what
        moved.
        """
        if not is_valid_uuid(career_entry_id):
            raise ValidationError(
                f"'{career_entry_id}' is not a valid career entry id."
            )

        entry = (
            CareerEntry.objects
            .select_related("sport")
            .filter(id=career_entry_id)
            .first()
        )

        if entry is None:
            raise ValidationError("That career entry no longer exists.")

        if entry.user_id != user.id:
            raise PermissionDenied(
                "You can only link achievements to your own career entries."
            )

        if entry.sport_id != sport.id:
            raise ValidationError(
                f"That career entry is a {entry.sport.name} stint, so it "
                f"cannot hold a {sport.name} achievement."
            )

        return entry

    @staticmethod
    def _clean_title(value) -> str:
        """What was won — "Golden Boot", "League Winner". Always required."""
        title = (value or "").strip()

        if not title:
            raise ValidationError(
                "A title is required (e.g. Golden Boot, League Winner)."
            )

        max_length = Achievement._meta.get_field("title").max_length
        if len(title) > max_length:
            raise ValidationError(
                f"Title cannot be longer than {max_length} characters."
            )

        return title

    @staticmethod
    def _clean_name(value) -> str:
        """Trim the free-text issuer name and hold it to the column width."""
        name = (value or "").strip()
        max_length = Achievement._meta.get_field("awarded_by_name").max_length

        if len(name) > max_length:
            raise ValidationError(
                f"Issuer name cannot be longer than {max_length} characters."
            )

        return name

    @staticmethod
    def _clean_text(value, field_name) -> str:
        """
        Trim any of the short free-text columns and hold it to its own width.

        Worth doing for ``image`` and ``reference_link`` in particular: a
        media URL carrying an actor-scoped object key runs long, and the
        difference between catching it here and not is a clean 400 versus an
        insert that fails in production only.
        """
        text = (value or "").strip()
        max_length = Achievement._meta.get_field(field_name).max_length

        if max_length is not None and len(text) > max_length:
            raise ValidationError(
                f"{field_name.replace('_', ' ').capitalize()} cannot be longer "
                f"than {max_length} characters."
            )

        return text

    @staticmethod
    def _clean_achieved_date(value):
        """
        The one date rule: an award has a day, and that day has happened.

        Deliberately not a DB CheckConstraint — those cannot reference ``now()``
        — so this is the only thing standing between the column and a medal won
        next year.
        """
        if value is None:
            raise ValidationError("An achievement date is required.")

        if value > timezone.localdate():
            raise ValidationError(
                "The achievement date cannot be in the future."
            )

        return value

    # =================================================================
    # VERIFICATION COUPLING
    # =================================================================

    @staticmethod
    def _clear_verification(achievement, status) -> None:
        """
        Move an achievement to ``status`` and drop the audit trail that only
        made sense while it was verified. Called whenever the claim changes
        underneath an existing confirmation.
        """
        achievement.verification_status = status
        achievement.verified_by = None
        achievement.verified_at = None

    # =================================================================
    # CREATE
    # =================================================================

    @staticmethod
    def create_achievement(actor, *, payload: dict) -> Achievement:
        """
        Add one award to the requester's profile.

        Verification follows what was claimed, never what the client asked for:

          * an achievement crediting an organization on Goatza starts
            ``pending`` — that org can confirm or reject it, and
            ``awarded_by_name`` is synced from them so the award survives the
            org being deleted
          * everything else starts ``self_reported`` — there is nobody to
            confirm it

        Two awards on the same day are ordinary (a cup final hands out a trophy
        and a man-of-the-match), so nothing here looks at the user's other rows
        beyond the caps.
        """
        user = AchievementService.require_user(actor)
        AchievementService._assert_under_cap(user)

        sport = AchievementService._resolve_sport(payload.get("sport"))

        awarded_by = None
        if payload.get("awarded_by"):
            awarded_by = AchievementService._resolve_organization(
                payload["awarded_by"]
            )

        # With an org linked, model.save() overwrites this from the org anyway;
        # the free-text value only stands on its own when there is no link. And
        # unlike a career entry, empty is a perfectly good answer.
        awarded_by_name = AchievementService._clean_name(
            payload.get("awarded_by_name")
        )

        career_entry = None
        if payload.get("career_entry"):
            career_entry = AchievementService._resolve_career_entry(
                user,
                sport,
                payload["career_entry"],
            )

        is_pinned = bool(payload.get("is_pinned"))
        if is_pinned:
            AchievementService._assert_pin_allowed(user)

        achievement = Achievement.objects.create(
            user=user,
            title=AchievementService._clean_title(payload.get("title")),
            achievement_type=(
                payload.get("achievement_type")
                or Achievement.AchievementType.INDIVIDUAL_AWARD
            ),
            sport=sport,
            description=(payload.get("description") or "").strip(),
            event_name=AchievementService._clean_text(
                payload.get("event_name"), "event_name"
            ),
            level=payload.get("level") or "",
            awarded_by=awarded_by,
            awarded_by_name=awarded_by_name,
            career_entry=career_entry,
            achieved_date=AchievementService._clean_achieved_date(
                payload.get("achieved_date")
            ),
            image=AchievementService._clean_text(payload.get("image"), "image"),
            image_public_id=AchievementService._clean_text(
                payload.get("image_public_id"), "image_public_id"
            ),
            reference_link=AchievementService._clean_text(
                payload.get("reference_link"), "reference_link"
            ),
            is_pinned=is_pinned,
            verification_status=(
                Achievement.VerificationStatus.PENDING
                if awarded_by
                else Achievement.VerificationStatus.SELF_REPORTED
            ),
        )

        if awarded_by:
            AchievementService._request_verification(achievement)

        return achievement

    # =================================================================
    # VERIFICATION REQUEST (→ the credited org)
    # =================================================================

    @staticmethod
    def _withdraw_request(achievement, organization_id) -> None:
        """
        Drop the outstanding "please verify this" notification held by an org
        the achievement no longer credits.

        The review QUEUE needs no cleanup — it is derived from ``awarded_by`` +
        ``pending``, so a re-credited award leaves it by itself. The
        notification is the part that lingers: without this, an org keeps a row
        inviting them to review a claim that has since moved elsewhere, and
        following it lands on a queue the award isn't in.

        Only the REQUEST type is removed. Any decisions that org already made
        are their record and stay put.
        """
        if not organization_id:
            return

        Notification.objects.filter(
            type=Notification.Type.ACHIEVEMENT_VERIFICATION_REQUEST,
            achievement=achievement,
            recipient_org_id=organization_id,
        ).delete()

    @staticmethod
    def _request_verification(achievement) -> None:
        """
        Ask the credited org to confirm the award.

        Deferred to after commit and never allowed to raise: a notification or
        FCM failure must not fail — or roll back — the owner's write. Same
        contract CareerEntryService._request_verification uses.
        """
        def _notify():
            try:
                NotificationService.achievement_verification_request(
                    actor_user=achievement.user,
                    achievement=achievement,
                )
            except Exception as exc:
                logger.warning(
                    "AchievementService | verification request notification "
                    f"failed | achievement_id={achievement.id} | {exc}"
                )

        transaction.on_commit(_notify)

    # =================================================================
    # UPDATE
    # =================================================================

    @staticmethod
    def update_achievement(actor, achievement_id, *, payload: dict) -> Achievement:
        """
        Edit one of the requester's achievements. PATCH semantics — only the
        keys actually present in ``payload`` are touched. This is also the
        pin/unpin path.

        Verification reacts to what changed, not to the fact that a PATCH
        happened:

          * linking (or relinking) an issuing organization → ``pending``, for
            the new org to confirm
          * unlinking one → ``self_reported``, since nobody is left to confirm
          * any other material change to a ``verified`` achievement → back to
            ``pending``, with ``verified_by`` / ``verified_at`` cleared
          * a description, reference-link, career-link or pin-only edit →
            status untouched (see MATERIAL_FIELDS)

        Re-sending a field with the value it already has is not a change, so a
        client that PATCHes the whole form back never loses a verification it
        did not actually alter.
        """
        user = AchievementService.require_user(actor)
        achievement = AchievementService._get_owned_achievement(
            user, achievement_id
        )

        if not payload:
            raise ValidationError("Nothing to update.")

        changed = set()

        # Snapshot before anything is touched — the notification decision at the
        # bottom is "did this achievement ENTER pending for this org", which
        # needs the state it started in.
        was_pending = (
            achievement.verification_status
            == Achievement.VerificationStatus.PENDING
        )
        previous_org_id = achievement.awarded_by_id

        # ── sport, and the career link that depends on it ─────────────
        sport = achievement.sport
        if "sport" in payload:
            sport = AchievementService._resolve_sport(payload["sport"])
            if sport.id != achievement.sport_id:
                changed.add("sport")

        achievement.sport = sport

        # Tri-state: absent (leave alone), an id (link), null (unlink).
        #
        # When the sport moved and the client did not resend the link, the
        # existing one is re-validated against the NEW sport rather than being
        # silently dropped. Careers clears positions in the equivalent case
        # because a position is meaningless outside its sport; a career entry is
        # a real row the owner deliberately pointed at, so the right answer is
        # to say the two no longer agree and let them decide which to fix.
        if "career_entry" in payload:
            raw_entry = payload["career_entry"]

            if raw_entry:
                career_entry = AchievementService._resolve_career_entry(
                    user, sport, raw_entry
                )
                achievement.career_entry = career_entry
            else:
                achievement.career_entry = None
        elif achievement.career_entry_id and "sport" in changed:
            AchievementService._resolve_career_entry(
                user, sport, achievement.career_entry_id
            )

        # ── issuing organization ─────────────────────────────────────
        org_status = None

        if "awarded_by" in payload:
            raw_org = payload["awarded_by"]

            if raw_org:
                organization = AchievementService._resolve_organization(raw_org)
                if organization.id != achievement.awarded_by_id:
                    achievement.awarded_by = organization
                    changed.add("awarded_by")
                    # A new issuer has not confirmed anything yet.
                    org_status = Achievement.VerificationStatus.PENDING
            elif achievement.awarded_by_id is not None:
                achievement.awarded_by = None
                changed.add("awarded_by")
                # Nobody left to confirm it; it is a personal claim again.
                org_status = Achievement.VerificationStatus.SELF_REPORTED

        # Only meaningful while the achievement stands on its own: with an org
        # linked the column is derived, and save() overwrites whatever is set
        # here. Honouring the payload anyway would count a doomed edit as a
        # material change and knock a verified award back to pending for
        # nothing.
        if "awarded_by_name" in payload and achievement.awarded_by_id is None:
            name = AchievementService._clean_name(payload["awarded_by_name"])
            if name != achievement.awarded_by_name:
                achievement.awarded_by_name = name
                changed.add("awarded_by_name")

        # ── plain fields ─────────────────────────────────────────────
        if "title" in payload:
            title = AchievementService._clean_title(payload["title"])
            if title != achievement.title:
                achievement.title = title
                changed.add("title")

        for field in ("achievement_type", "level"):
            if field in payload:
                value = payload[field] or ""
                if value != getattr(achievement, field):
                    setattr(achievement, field, value)
                    changed.add(field)

        for field in ("event_name", "image", "reference_link"):
            if field in payload:
                value = AchievementService._clean_text(payload[field], field)
                if value != getattr(achievement, field):
                    setattr(achievement, field, value)
                    changed.add(field)

        # Carried alongside `image` but never material on its own — it is the
        # storage handle for the same upload, not a separate claim.
        if "image_public_id" in payload:
            achievement.image_public_id = AchievementService._clean_text(
                payload["image_public_id"], "image_public_id"
            )

        if "description" in payload:
            # Deliberately not added to `changed` — see MATERIAL_FIELDS.
            achievement.description = (payload["description"] or "").strip()

        # ── date ─────────────────────────────────────────────────────
        if "achieved_date" in payload:
            achieved_date = AchievementService._clean_achieved_date(
                payload["achieved_date"]
            )
            if achieved_date != achievement.achieved_date:
                achievement.achieved_date = achieved_date
                changed.add("achieved_date")

        # ── pin ──────────────────────────────────────────────────────
        # Not material, but capped: checked against the user's OTHER pins so
        # re-pinning something already pinned never trips its own cap.
        if "is_pinned" in payload:
            is_pinned = bool(payload["is_pinned"])
            if is_pinned and not achievement.is_pinned:
                AchievementService._assert_pin_allowed(
                    user, exclude_id=achievement.id
                )
            achievement.is_pinned = is_pinned

        # ── verification fallout ─────────────────────────────────────
        if org_status is not None:
            AchievementService._clear_verification(achievement, org_status)
        elif (
            achievement.verification_status
            == Achievement.VerificationStatus.VERIFIED
            and changed & set(AchievementService.MATERIAL_FIELDS)
        ):
            AchievementService._clear_verification(
                achievement,
                Achievement.VerificationStatus.PENDING
            )

        # Full save: too many fields are conditionally touched for update_fields
        # to be worth the risk of forgetting one. save() also re-syncs
        # awarded_by_name from the linked org.
        achievement.save()

        # ── the old issuer's request is no longer true ───────────────
        # Re-credited to a different org, or unlinked entirely. The previous org
        # is no longer being asked anything, so their outstanding request
        # notification goes with the link. Their queue clears on its own (it
        # filters on the FK), but the notification would otherwise survive as a
        # dead invitation to review.
        if achievement.awarded_by_id != previous_org_id:
            AchievementService._withdraw_request(achievement, previous_org_id)

        # ── re-request verification ──────────────────────────────────
        # Only when the achievement ENTERED pending for the org it now credits:
        # a first link, a verified award knocked back by a material edit, or a
        # re-link onto a different org. One that was already pending for the
        # same org and stays pending has not changed anything the reviewer can
        # act on, so it must not re-notify.
        entered_pending = (
            achievement.awarded_by_id is not None
            and achievement.verification_status
            == Achievement.VerificationStatus.PENDING
            and (not was_pending or achievement.awarded_by_id != previous_org_id)
        )

        if entered_pending:
            AchievementService._request_verification(achievement)

        return achievement

    # =================================================================
    # DELETE
    # =================================================================

    @staticmethod
    def delete_achievement(actor, achievement_id) -> uuid.UUID:
        """
        Remove one achievement for good.

        Hard delete on purpose: this is structured profile data like a career
        entry, not user-authored content like a post. Nothing references it,
        nobody can have replied to it, and an award you removed should not
        linger in the tables a recruiter searches. The Notification FK CASCADE
        takes the deep links with it. Returns the id that was deleted.
        """
        user = AchievementService.require_user(actor)
        achievement = AchievementService._get_owned_achievement(
            user, achievement_id
        )

        deleted_id = achievement.id
        achievement.delete()

        return deleted_id
