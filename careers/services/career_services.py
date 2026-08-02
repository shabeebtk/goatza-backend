"""
Write path for career entries.

Views stay thin: they hand ``request.actor`` and the shaped payload straight in
and every rule is applied here — user actors only, own entries only, the
organization/verification coupling, and the sport ↔ position integrity check.

The first argument is the resolved ``core.actor.Actor``, not a User. "Your own
career" is not just an ownership check, it also means *acting as yourself*: a
person acting through one of their organizations is not editing their personal
history, and only the actor knows which it was.

Failures raise DRF exceptions so the message reaches the client unchanged:

  * PermissionDenied (403) — acting as an organization, or not your entry
  * NotFound (404)         — no such entry
  * ValidationError (400)  — everything else (bad dates, position/sport
    mismatch, unknown organization or sport)
"""

import logging
import uuid

from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.exceptions import (
    NotFound,
    PermissionDenied,
    ValidationError,
)

from accounts.models import User
from careers.models import CareerEntry
from notifications.models import Notification
from notifications.services.notification_service import NotificationService
from organization.models import Organization
from recruitments.models import (
    RecruitmentApplication,
    RecruitmentApplicationStatusHistory,
)
from sports.models import Sport, SportPosition
from utils.validations import is_valid_uuid

logger = logging.getLogger(__name__)


class CareerEntryService:

    # A career is a highlight reel, not an exhaustive CV. The cap keeps the
    # profile section readable and bounds the verification queues an org can be
    # pointed at by one person. Enforced here rather than in the serializer so
    # every write path — manual create and the recruitment prompt — shares it.
    MAX_ENTRIES = 10

    # Editing any of these is a claim about the stint itself, so a verified
    # entry has to be re-checked. ``description`` is deliberately absent —
    # rewording your own blurb does not invalidate a club's confirmation.
    MATERIAL_FIELDS = (
        "organization",
        "organization_name",
        "sport",
        "title",
        "entry_type",
        "squad_level",
        "age_group",
        "positions",
        "start_date",
        "end_date",
        "is_current",
    )

    # =================================================================
    # GUARDS
    # =================================================================

    @staticmethod
    def require_user(actor) -> User:
        """
        The single write gate. Returns the owning user, or raises
        PermissionDenied (→ 403) when the actor may not manage career entries:
        signed out, or acting as an organization.

        Public because the write views call it *before* validating the body:
        someone who may not write at all should get 403, not a critique of a
        payload that was never going to be accepted. Every write here calls it
        again anyway, so the rule still lives in exactly one place.

        No role check — coaches and scouts have careers too ("Head Coach",
        "Youth Scout" are listed job titles), unlike highlights which are
        players-only.
        """
        if actor is None or not actor.is_user or actor.user is None:
            if actor is not None and actor.is_org:
                raise PermissionDenied(
                    "Career entries belong to a person, not an organization. "
                    "Switch to your personal account to manage yours."
                )
            raise PermissionDenied(
                "You must be signed in to manage career entries."
            )

        return actor.user

    @staticmethod
    def _assert_under_cap(user) -> None:
        """
        Refuse a create once the user is at MAX_ENTRIES.

        Checked on every create path. Not a race-proof lock — two simultaneous
        creates could both pass — but the cap is a product limit, not a
        correctness invariant, and one extra row is not worth serializing every
        write behind a row lock.
        """
        if CareerEntry.objects.filter(user=user).count() >= CareerEntryService.MAX_ENTRIES:
            raise ValidationError(
                f"You can have up to {CareerEntryService.MAX_ENTRIES} career "
                f"entries. Delete one to add another."
            )

    @staticmethod
    def _get_owned_entry(user, entry_id) -> CareerEntry:
        """
        Load one entry and prove it belongs to ``user``. A missing entry reads
        as gone (404); someone else's reads as forbidden (403).
        """
        if not is_valid_uuid(entry_id):
            raise ValidationError(f"'{entry_id}' is not a valid career entry id.")

        entry = (
            CareerEntry.objects
            .filter(id=entry_id)
            .first()
        )

        if entry is None:
            raise NotFound("Career entry not found.")

        if entry.user_id != user.id:
            raise PermissionDenied(
                "You can only manage your own career entries."
            )

        return entry

    # =================================================================
    # FIELD RESOLUTION / CLEANING
    # =================================================================

    @staticmethod
    def _resolve_sport(sport_id) -> Sport:
        """A career entry is always about one sport, and it has to be a real one."""
        if not sport_id or not is_valid_uuid(sport_id):
            raise ValidationError("A valid sport is required.")

        sport = Sport.objects.filter(id=sport_id).first()

        if sport is None:
            raise ValidationError("That sport does not exist.")

        return sport

    @staticmethod
    def _resolve_organization(organization_id) -> Organization:
        """
        Resolve the club/academy being claimed. Only reached when the caller
        actually sent an id — an entry with no organization is normal (clubs
        that are not on Goatza) and is carried by ``organization_name`` alone.
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
            raise ValidationError("That club is no longer on Goatza.")

        return organization

    @staticmethod
    def _resolve_positions(sport, position_ids) -> list:
        """
        Resolve the positions played in this stint and prove every one of them
        belongs to ``sport``. A goalkeeper cannot be a position in a basketball
        entry, and one bad id fails the whole write rather than being dropped
        silently.

        One query regardless of how many ids were sent.
        """
        if not position_ids:
            return []

        if not isinstance(position_ids, (list, tuple, set)):
            raise ValidationError("Positions must be a list of position ids.")

        wanted = []
        for raw_id in position_ids:
            if not is_valid_uuid(raw_id):
                raise ValidationError(f"'{raw_id}' is not a valid position id.")
            wanted.append(uuid.UUID(str(raw_id)))

        positions = list(
            SportPosition.objects
            .filter(id__in=set(wanted))
            .select_related("sport")
        )

        found = {position.id for position in positions}
        missing = set(wanted) - found
        if missing:
            raise ValidationError("Some of those positions no longer exist.")

        wrong_sport = [
            position.name
            for position in positions
            if position.sport_id != sport.id
        ]
        if wrong_sport:
            raise ValidationError(
                f"{', '.join(sorted(wrong_sport))} "
                f"{'is' if len(wrong_sport) == 1 else 'are'} not a "
                f"{sport.name} position."
            )

        return positions

    @staticmethod
    def _clean_name(value) -> str:
        """Trim the free-text club name and hold it to the column width."""
        name = (value or "").strip()
        max_length = CareerEntry._meta.get_field("organization_name").max_length

        if len(name) > max_length:
            raise ValidationError(
                f"Organization name cannot be longer than {max_length} characters."
            )

        return name

    @staticmethod
    def _clean_title(value) -> str:
        """The role held — "Player", "Captain", "Head Coach". Always required."""
        title = (value or "").strip()

        if not title:
            raise ValidationError("A title is required (e.g. Player, Head Coach).")

        max_length = CareerEntry._meta.get_field("title").max_length
        if len(title) > max_length:
            raise ValidationError(
                f"Title cannot be longer than {max_length} characters."
            )

        return title

    @staticmethod
    def _clean_dates(start_date, end_date, is_current):
        """
        Apply the two date rules and return the pair to store.

        ``is_current`` wins over ``end_date``: a stint you are still in has no
        end, so the end date is cleared rather than the request being rejected
        — the client's "still here" checkbox and its date picker are two
        controls for the same fact, and the checkbox is the newer intent.
        Mirrors the DB CheckConstraint, which would otherwise fail the insert.
        """
        if start_date is None:
            raise ValidationError("A start date is required.")

        if is_current:
            return start_date, None

        if end_date is not None and end_date < start_date:
            raise ValidationError("The end date cannot be before the start date.")

        return start_date, end_date

    # =================================================================
    # VERIFICATION COUPLING
    # =================================================================

    @staticmethod
    def _clear_verification(entry, status) -> None:
        """
        Move an entry to ``status`` and drop the audit trail that only made
        sense while it was verified. Called whenever a claim changes underneath
        an existing confirmation.
        """
        entry.verification_status = status
        entry.verified_by = None
        entry.verified_at = None

    # =================================================================
    # CREATE
    # =================================================================

    @staticmethod
    def create_entry(actor, *, payload: dict) -> CareerEntry:
        """
        Add one stint to the requester's career history.

        Verification follows what was claimed, never what the client asked for:

          * an entry linked to an organization on Goatza starts ``pending`` —
            the club can confirm or reject it, and ``organization_name`` is
            synced from the org so the entry survives the org being deleted
          * a free-text entry starts ``self_reported`` — there is nobody to
            confirm it

        Overlapping entries are allowed on purpose (a loan runs inside a club
        contract), so nothing here looks at the user's other entries.
        """
        user = CareerEntryService.require_user(actor)
        CareerEntryService._assert_under_cap(user)

        sport = CareerEntryService._resolve_sport(payload.get("sport"))

        organization = None
        if payload.get("organization"):
            organization = CareerEntryService._resolve_organization(
                payload["organization"]
            )

        # With an org linked, model.save() overwrites this from the org anyway;
        # the free-text value only stands on its own when there is no link.
        organization_name = CareerEntryService._clean_name(
            payload.get("organization_name")
        )
        if organization is None and not organization_name:
            raise ValidationError(
                "Pick a club, or type the club or school name."
            )

        positions = CareerEntryService._resolve_positions(
            sport,
            payload.get("positions")
        )

        is_current = bool(payload.get("is_current"))
        start_date, end_date = CareerEntryService._clean_dates(
            payload.get("start_date"),
            payload.get("end_date"),
            is_current,
        )

        entry = CareerEntry.objects.create(
            user=user,
            organization=organization,
            organization_name=organization_name,
            sport=sport,
            title=CareerEntryService._clean_title(payload.get("title")),
            entry_type=payload.get("entry_type") or CareerEntry.EntryType.CLUB_TEAM,
            squad_level=payload.get("squad_level") or "",
            age_group=(payload.get("age_group") or "").strip(),
            start_date=start_date,
            end_date=end_date,
            is_current=is_current,
            description=(payload.get("description") or "").strip(),
            verification_status=(
                CareerEntry.VerificationStatus.PENDING
                if organization
                else CareerEntry.VerificationStatus.SELF_REPORTED
            ),
            source=CareerEntry.Source.MANUAL,
        )

        if positions:
            entry.positions.set(positions)

        if organization:
            CareerEntryService._request_verification(entry)

        return entry

    # =================================================================
    # CREATE FROM A SELECTED APPLICATION
    # =================================================================

    # Recruitment types that clearly imply something other than a club stint.
    # Anything not listed falls back to club_team — the map only fires where the
    # recruitment type genuinely settles the question.
    ENTRY_TYPE_BY_RECRUITMENT_TYPE = {
        "scholarship": CareerEntry.EntryType.ACADEMY,
    }

    @staticmethod
    def create_from_application(actor, application_id, *, payload=None):
        """
        Turn a selection into a career entry, prefilled from the recruitment.

        Returns ``(entry, created)``. ``created`` is False when an entry for
        this application already existed — the caller answers 200 rather than
        201 and nothing is written. The Stage 1 partial unique constraint on
        ``recruitment_application`` is the backstop for the race where two
        requests get past that check together.

        The entry lands ``verified``: it did not come from the player's word,
        it came out of the org's own pipeline, and the org is the party that
        would otherwise be asked to confirm it. ``verified_by`` is the member
        who moved the application to selected, read back from the status
        history — null if that row is gone or the member has since been removed.

        ``payload`` may override ``title``, ``start_date`` and ``description``;
        everything else is derived, and the overrides go through the same
        cleaning the manual create uses.
        """
        user = CareerEntryService.require_user(actor)
        payload = payload or {}

        application = CareerEntryService._get_own_application(user, application_id)

        # Idempotency check before the status gate: an entry created from an
        # application that has since moved off `selected` is still that
        # player's entry, and asking for it again must not start failing.
        existing = (
            CareerEntry.objects
            .filter(recruitment_application=application)
            .first()
        )
        if existing is not None:
            return existing, False

        if application.status != RecruitmentApplication.Status.SELECTED:
            raise ValidationError(
                "You can only add this to your career once the club has "
                "selected you."
            )

        # After the idempotency check, so someone at the cap can still fetch an
        # entry they already made from this application.
        CareerEntryService._assert_under_cap(user)

        recruitment = application.recruitment
        organization = recruitment.organization
        sport = recruitment.sport

        # The position applied for, when there was one. Routed through the same
        # resolver as a manual create so a position that does not belong to the
        # recruitment's sport is caught rather than silently attached.
        positions = []
        if application.applied_position_id:
            positions = CareerEntryService._resolve_positions(
                sport,
                [application.applied_position_id]
            )

        selected_at, reviewer = CareerEntryService._selection_facts(application)

        start_date = payload.get("start_date") or CareerEntryService._start_date_for(
            recruitment,
            selected_at,
        )
        # is_current is always True here, so _clean_dates drops any end date —
        # a stint you were just selected for has not ended.
        start_date, end_date = CareerEntryService._clean_dates(
            start_date,
            None,
            True,
        )

        title = CareerEntryService._clean_title(payload.get("title") or "Player")

        now = timezone.now()

        try:
            with transaction.atomic():
                entry = CareerEntry.objects.create(
                    user=user,
                    organization=organization,
                    # Overwritten from the org by save(); set so the column is
                    # never momentarily empty.
                    organization_name=organization.name[:150],
                    sport=sport,
                    title=title,
                    entry_type=CareerEntryService.ENTRY_TYPE_BY_RECRUITMENT_TYPE.get(
                        recruitment.recruitment_type,
                        CareerEntry.EntryType.CLUB_TEAM,
                    ),
                    start_date=start_date,
                    end_date=end_date,
                    is_current=True,
                    description=(payload.get("description") or "").strip(),
                    verification_status=CareerEntry.VerificationStatus.VERIFIED,
                    verified_by=reviewer,
                    verified_at=now,
                    source=CareerEntry.Source.RECRUITMENT,
                    recruitment_application=application,
                )
        except IntegrityError:
            # Lost the race against a concurrent identical request — the unique
            # constraint did its job, so hand back the row that won.
            entry = (
                CareerEntry.objects
                .filter(recruitment_application=application)
                .first()
            )
            if entry is None:
                raise
            return entry, False

        if positions:
            entry.positions.set(positions)

        # No verification request: the org already decided this by selecting
        # them. It only enters that queue if the player edits it materially
        # later, which update_entry handles.
        return entry, True

    @staticmethod
    def _get_own_application(user, application_id):
        """Load the application and prove the requester is the applicant."""
        if not is_valid_uuid(application_id):
            raise ValidationError(
                f"'{application_id}' is not a valid application id."
            )

        application = (
            RecruitmentApplication.objects
            .select_related(
                "recruitment",
                "recruitment__organization",
                "recruitment__sport",
            )
            .filter(id=application_id)
            .first()
        )

        if application is None:
            raise NotFound("Application not found.")

        if application.applicant_id != user.id:
            raise PermissionDenied(
                "You can only add your own applications to your career."
            )

        return application

    @staticmethod
    def _selection_facts(application):
        """
        Read back WHEN the application became selected and WHO moved it, from
        the status history.

        Returns ``(selected_on, reviewer_user)``, either of which may be None:
        the history row can be missing (a status set before history was kept,
        or by a path that does not write one) and ``changed_by`` goes null when
        the member leaves the org. Neither is fatal — the caller falls back.
        """
        history = (
            RecruitmentApplicationStatusHistory.objects
            .filter(
                application=application,
                to_status=RecruitmentApplication.Status.SELECTED,
            )
            .select_related("changed_by")
            .order_by("-created_at")
            .first()
        )

        if history is None:
            return None, None

        reviewer = history.changed_by.user if history.changed_by else None

        return timezone.localdate(history.created_at), reviewer

    @staticmethod
    def _start_date_for(recruitment, selected_on):
        """
        When the stint started, best evidence first: the trial/event date the
        org published, then the day they selected the player, then today.
        """
        if recruitment.event_date:
            return timezone.localdate(recruitment.event_date)

        if selected_on:
            return selected_on

        return timezone.localdate()

    # =================================================================
    # VERIFICATION REQUEST (→ the tagged org)
    # =================================================================

    @staticmethod
    def _withdraw_request(entry, organization_id) -> None:
        """
        Drop the outstanding "please verify this" notification held by an org
        the entry no longer names.

        The review QUEUE needs no cleanup — it is derived from
        ``organization`` + ``pending``, so a re-tagged entry leaves it by
        itself. The notification is the part that lingers: without this, a club
        keeps a row inviting them to review a claim that has since moved to a
        different club, and following it lands on a queue the entry isn't in.

        Only the REQUEST type is removed. The decisions that club already made
        are their record and stay put.
        """
        if not organization_id:
            return

        Notification.objects.filter(
            type=Notification.Type.CAREER_VERIFICATION_REQUEST,
            career_entry=entry,
            recipient_org_id=organization_id,
        ).delete()

    @staticmethod
    def _request_verification(entry) -> None:
        """
        Ask the tagged org to confirm the entry.

        Deferred to after commit and never allowed to raise: a notification or
        FCM failure must not fail — or roll back — the player's write. Same
        contract ApplicationService.apply uses for its org notification.
        """
        def _notify():
            try:
                NotificationService.career_verification_request(
                    actor_user=entry.user,
                    career_entry=entry,
                )
            except Exception as exc:
                logger.warning(
                    "CareerEntryService | verification request notification "
                    f"failed | entry_id={entry.id} | {exc}"
                )

        transaction.on_commit(_notify)

    # =================================================================
    # UPDATE
    # =================================================================

    @staticmethod
    def update_entry(actor, entry_id, *, payload: dict) -> CareerEntry:
        """
        Edit one of the requester's entries. PATCH semantics — only the keys
        actually present in ``payload`` are touched.

        Verification reacts to what changed, not to the fact that a PATCH
        happened:

          * linking (or relinking) an organization → ``pending``, for the new
            club to confirm
          * unlinking one → ``self_reported``, since nobody is left to confirm
          * any other material change to a ``verified`` entry → back to
            ``pending``, with ``verified_by`` / ``verified_at`` cleared
          * a description-only edit → status untouched

        Re-sending a field with the value it already has is not a change, so a
        client that PATCHes the whole form back never loses a verification it
        did not actually alter.
        """
        user = CareerEntryService.require_user(actor)
        entry = CareerEntryService._get_owned_entry(user, entry_id)

        if not payload:
            raise ValidationError("Nothing to update.")

        changed = set()

        # Snapshot before anything is touched — the notification decision at the
        # bottom is "did this entry ENTER pending for this org", which needs the
        # state it started in.
        was_pending = (
            entry.verification_status == CareerEntry.VerificationStatus.PENDING
        )
        previous_org_id = entry.organization_id

        # ── sport, and the positions that depend on it ────────────────
        sport = entry.sport
        if "sport" in payload:
            sport = CareerEntryService._resolve_sport(payload["sport"])
            if sport.id != entry.sport_id:
                changed.add("sport")

        if "positions" in payload:
            positions = CareerEntryService._resolve_positions(
                sport,
                payload["positions"]
            )
        elif "sport" in payload and sport.id != entry.sport_id:
            # The sport moved but the client did not resend positions — the old
            # ones now belong to a different sport, so they cannot carry over.
            positions = []
        else:
            positions = None

        entry.sport = sport

        # ── organization link ────────────────────────────────────────
        # Tri-state: absent (leave alone), an id (link), null (unlink).
        org_status = None

        if "organization" in payload:
            raw_org = payload["organization"]

            if raw_org:
                organization = CareerEntryService._resolve_organization(raw_org)
                if organization.id != entry.organization_id:
                    entry.organization = organization
                    changed.add("organization")
                    # A new club has not confirmed anything yet.
                    org_status = CareerEntry.VerificationStatus.PENDING
            elif entry.organization_id is not None:
                entry.organization = None
                changed.add("organization")
                # Nobody left to confirm it; it is a personal claim again.
                org_status = CareerEntry.VerificationStatus.SELF_REPORTED

        # Only meaningful while the entry stands on its own: with an org linked
        # the column is derived, and save() overwrites whatever is set here.
        # Honouring the payload anyway would count a doomed edit as a material
        # change and knock a verified entry back to pending for nothing.
        if "organization_name" in payload and entry.organization_id is None:
            name = CareerEntryService._clean_name(payload["organization_name"])
            if name != entry.organization_name:
                entry.organization_name = name
                changed.add("organization_name")

        # Free text is the only thing holding an unlinked entry together.
        if entry.organization_id is None and not entry.organization_name:
            raise ValidationError(
                "Pick a club, or type the club or school name."
            )

        # ── plain fields ─────────────────────────────────────────────
        if "title" in payload:
            title = CareerEntryService._clean_title(payload["title"])
            if title != entry.title:
                entry.title = title
                changed.add("title")

        for field in ("entry_type", "squad_level"):
            if field in payload:
                value = payload[field] or ""
                if value != getattr(entry, field):
                    setattr(entry, field, value)
                    changed.add(field)

        if "age_group" in payload:
            age_group = (payload["age_group"] or "").strip()
            if age_group != entry.age_group:
                entry.age_group = age_group
                changed.add("age_group")

        if "description" in payload:
            # Deliberately not added to `changed` — see MATERIAL_FIELDS.
            entry.description = (payload["description"] or "").strip()

        # ── dates ────────────────────────────────────────────────────
        is_current = (
            bool(payload["is_current"])
            if "is_current" in payload
            else entry.is_current
        )
        start_date = payload.get("start_date", entry.start_date)
        end_date = payload.get("end_date", entry.end_date)

        start_date, end_date = CareerEntryService._clean_dates(
            start_date,
            end_date,
            is_current,
        )

        for field, value in (
            ("start_date", start_date),
            ("end_date", end_date),
            ("is_current", is_current),
        ):
            if value != getattr(entry, field):
                setattr(entry, field, value)
                changed.add(field)

        # ── positions ────────────────────────────────────────────────
        if positions is not None:
            before = set(entry.positions.values_list("id", flat=True))
            after = {position.id for position in positions}
            if before != after:
                changed.add("positions")

        # ── verification fallout ─────────────────────────────────────
        if org_status is not None:
            CareerEntryService._clear_verification(entry, org_status)
        elif (
            entry.verification_status == CareerEntry.VerificationStatus.VERIFIED
            and changed & set(CareerEntryService.MATERIAL_FIELDS)
        ):
            CareerEntryService._clear_verification(
                entry,
                CareerEntry.VerificationStatus.PENDING
            )

        # Full save: too many fields are conditionally touched for update_fields
        # to be worth the risk of forgetting one. save() also re-syncs
        # organization_name from the linked org.
        entry.save()

        if positions is not None:
            entry.positions.set(positions)

        # ── the old club's request is no longer true ─────────────────
        # Re-tagged onto a different club, or unlinked entirely. The previous
        # club is no longer being asked anything, so their outstanding request
        # notification goes with the link. Their queue clears on its own (it
        # filters on the FK), but the notification would otherwise survive as a
        # dead invitation to review.
        if entry.organization_id != previous_org_id:
            CareerEntryService._withdraw_request(entry, previous_org_id)

        # ── re-request verification ──────────────────────────────────
        # Only when the entry ENTERED pending for the org it now names: a
        # first tag, a verified entry knocked back by a material edit, or a
        # re-tag onto a different club. An entry that was already pending for
        # the same org and stays pending has not changed anything the reviewer
        # can act on, so it must not re-notify.
        entered_pending = (
            entry.organization_id is not None
            and entry.verification_status == CareerEntry.VerificationStatus.PENDING
            and (not was_pending or entry.organization_id != previous_org_id)
        )

        if entered_pending:
            CareerEntryService._request_verification(entry)

        return entry

    # =================================================================
    # DELETE
    # =================================================================

    @staticmethod
    def delete_entry(actor, entry_id) -> uuid.UUID:
        """
        Remove one entry for good.

        Hard delete on purpose: this is structured profile data like UserSport,
        not user-authored content like a post. Nothing references it, nobody
        can have replied to it, and a career you removed should not linger in
        the tables a recruiter searches. Returns the id that was deleted.
        """
        user = CareerEntryService.require_user(actor)
        entry = CareerEntryService._get_owned_entry(user, entry_id)

        deleted_id = entry.id
        entry.delete()

        return deleted_id
