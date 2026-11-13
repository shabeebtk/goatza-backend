"""
Who is in a squad, how they got there, and how they stop being in it.

THE LOCK ORDER. Every write runs in one transaction and takes the squad row's
lock FIRST (``get_org_squad(lock=True)``), then — on the club's side — the
acting member (``relock_actor``), then membership rows, then career entries.
Two writers on one squad therefore queue on the squad and nowhere else first,
and a move locks its two squads in id order. Counts (the caps) are separate
statements AFTER the lock: under READ COMMITTED a waiter never sees a row
inserted while it waited unless it asks again.

THE COUNTERS. ``members_count`` (ACTIVE) and ``pending_count`` (PENDING) move
with F() in the same transaction as the row; the bulk paths and account
deletion finish with ``recount``.

READ-TIME EXPIRY. An INVITED row past ``expires_at`` IS expired. It is deleted
the moment a new live row for the same (squad, user) is needed, which is what
frees the partial unique constraint.

HISTORY. LEFT / REMOVED / MOVED rows are kept. Declined and withdrawn requests,
and declined / cancelled / expired invites, are DELETED: nothing was ever on
the player's profile.

SIDE EFFECTS AFTER COMMIT. Every notification is registered with
``transaction.on_commit(..., robust=True)``: a push that fails must neither
fail nor roll back a change the club has already made.

THE CAREER RULE (``_attach_career_entry`` / ``_release_career_entry``). Every
ACTIVE membership is backed by a VERIFIED CareerEntry at the club — one entry
per player per club + sport, shared by every squad of that club they are in.
"""

import logging
from datetime import timedelta

from django.db import IntegrityError, models, transaction
from django.db.models import F, Q
from django.db.models.functions import Greatest
from django.utils import timezone

from apps.accounts.models import User
from apps.careers.models import CareerEntry
from apps.guardians.permissions import BLOCKING_STATUSES
from apps.moderation.selectors.blocked_filters import blocked_id_sets
from apps.notifications.services.notification_service import NotificationService
from apps.organization.models import Organization, OrganizationMember
from apps.organization.permissions import OrgPermission, has_org_permission
from apps.organization.services.organization_member_service import _org_is_usable
from apps.recruitments.models import RecruitmentApplication
from apps.sports.models import SportPosition, UserSportPosition
from apps.squads.errors import (
    CANNOT_ADD_MESSAGE,
    SquadError,
    invite_closed,
    member_not_found,
    squad_not_found,
)
from apps.squads.models import LIVE_MEMBER_STATUSES, Squad, SquadMember
from apps.squads.selectors.squad_selectors import primary_positions_for
from apps.squads.services.public_cache import bust_public_squads
from apps.squads.services.guards import (
    check_player,
    get_org_squad,
    relock_actor,
    require_squads_permission,
)
from utils.validations import is_valid_uuid

logger = logging.getLogger(__name__)

MAX_ACTIVE_PER_SQUAD = 100
MAX_PENDING_PER_SQUAD = 100
MAX_OPEN_INVITES_PER_SQUAD = 50
INVITE_TTL = timedelta(days=SquadMember.INVITE_TTL_DAYS)
MAX_BULK = 50
NOTES_MAX = SquadMember.NOTES_MAX_LENGTH

CANDIDATE_MIN_QUERY = 2
CANDIDATE_MAX_QUERY = 100
CANDIDATES_LIMIT = 20

Status = SquadMember.Status
Source = SquadMember.Source

REVIEW = OrgPermission.SQUADS_REVIEW

SQUAD_FULL_MESSAGE = (
    f"This squad is full. A squad can have up to {MAX_ACTIVE_PER_SQUAD} players."
)
LINK_CLOSED_MESSAGE = "This invite link isn't available any more."
NOT_PENDING_MESSAGE = "This request is no longer pending."
NOT_ACTIVE_MESSAGE = "This player isn't in the squad any more."
INVALID_POSITION_MESSAGE = "Choose a position for this squad's sport."
INVALID_JERSEY_MESSAGE = "Jersey numbers are 0 to 99."
JERSEY_TAKEN_MESSAGE = "Another player in this squad has that number."

# Keyed by the plain value: looked up with ``organization.type``, a str.
ENTRY_TYPE_BY_ORG_TYPE = {
    Organization.Type.ACADEMY.value: CareerEntry.EntryType.ACADEMY,
    Organization.Type.SCHOOL.value: CareerEntry.EntryType.SCHOOL_COLLEGE,
    Organization.Type.CLUB.value: CareerEntry.EntryType.CLUB_TEAM,
    Organization.Type.TEAM.value: CareerEntry.EntryType.CLUB_TEAM,
}

# Why a player can't be added to the squad they are moving to, by their live
# status there. Keyed by the plain value: looked up with ``row.status``.
ALREADY_IN_TARGET = {
    Status.ACTIVE.value: ("already_member", "This player is already in that squad."),
    Status.INVITED.value: (
        "already_invited", "This player already has an invite to that squad."
    ),
    Status.PENDING.value: (
        "already_requested", "This player has already asked to join that squad."
    ),
}


# =====================================================================
# SMALL HELPERS
# =====================================================================

def _after_commit(callback, *args):
    """
    A side effect that must not fail, or roll back, the write it follows.

    A real function, not ``functools.partial``: Django's robust on_commit logs
    a failure with ``callback.__qualname__``, which a partial does not have —
    the logging itself would then raise, and the failure would escape after all.
    """
    def run_after_commit():
        callback(*args)

    run_after_commit.__qualname__ = getattr(callback, "__qualname__", "squads.after_commit")
    transaction.on_commit(run_after_commit, robust=True)


def _today():
    return timezone.localdate()


def _is_open_invite(row, now=None) -> bool:
    return (
        row.status == Status.INVITED
        and row.expires_at is not None
        and row.expires_at > (now or timezone.now())
    )


def _count(squad, *args, **filters) -> int:
    """A FRESH statement — call it after the squad lock, never before."""
    return SquadMember.objects.filter(*args, squad=squad, **filters).count()


def _open_invites_q(now=None):
    return Q(status=Status.INVITED, expires_at__gt=now or timezone.now())


def _bump(squad, members=0, pending=0):
    """Move the counters with F(), floored at 0 so a drifted row cannot go negative."""
    updates = {}
    if members:
        updates["members_count"] = Greatest(
            F("members_count") + members, 0, output_field=models.IntegerField()
        )
    if pending:
        updates["pending_count"] = Greatest(
            F("pending_count") + pending, 0, output_field=models.IntegerField()
        )
    if updates:
        Squad.objects.filter(id=squad.id).update(**updates)

    # The ACTIVE roster changed, so the anonymous showcase did too. Every path
    # that adds or ends an active row comes through here (or through recount).
    if members:
        bust_public_squads(squad.organization_id)


def _clean_position(squad, value):
    """A SportPosition of the squad's sport, or None for "no position"."""
    if value in (None, ""):
        return None

    position = (
        SportPosition.objects.filter(id=value, sport_id=squad.sport_id).first()
        if is_valid_uuid(str(value)) else None
    )
    if position is None:
        raise SquadError(
            "invalid_position", INVALID_POSITION_MESSAGE, field="position_id"
        )
    return position


def _clean_jersey(value):
    """0–99, or None. Accepts a JSON number or a digit string."""
    if value is None or value == "":
        return None

    # bool is an int subclass — True would otherwise be jersey 1.
    if isinstance(value, bool):
        raise SquadError("invalid_jersey", INVALID_JERSEY_MESSAGE, field="jersey_number")

    if isinstance(value, str):
        value = value.strip()
        if not (value.isascii() and value.isdigit()):
            raise SquadError(
                "invalid_jersey", INVALID_JERSEY_MESSAGE, field="jersey_number"
            )
        value = int(value)

    if not isinstance(value, int) or not 0 <= value <= 99:
        raise SquadError("invalid_jersey", INVALID_JERSEY_MESSAGE, field="jersey_number")

    return value


def _clean_bool(value, field):
    if not isinstance(value, bool):
        raise SquadError("invalid_field", f"{field} must be true or false.", field=field)
    return value


def _clean_batch(values, field):
    if not isinstance(values, list) or not 1 <= len(values) <= MAX_BULK:
        raise SquadError(
            "invalid_field", f"Choose 1 to {MAX_BULK} at a time.", field=field
        )
    return values


def _jersey_taken(squad, number, exclude_id=None) -> bool:
    if number is None:
        return False
    rows = SquadMember.objects.filter(
        squad=squad, status=Status.ACTIVE, jersey_number=number
    )
    if exclude_id:
        rows = rows.exclude(id=exclude_id)
    return rows.exists()


def _clear_captain(squad, exclude_id=None):
    """Before a new captain is set: the partial unique index allows one."""
    rows = SquadMember.objects.filter(squad=squad, status=Status.ACTIVE, is_captain=True)
    if exclude_id:
        rows = rows.exclude(id=exclude_id)
    rows.update(is_captain=False, updated_at=timezone.now())


def _primary_position(user, sport_id):
    """The player's own primary position for this sport, or None."""
    link = (
        UserSportPosition.objects
        .filter(
            user=user,
            sport_id=sport_id,
            is_primary=True,
            position__sport_id=sport_id,
        )
        .select_related("position")
        .first()
    )
    return link.position if link else None


def _clean_application(organization, player, application_id):
    """
    A Selected application of this player to one of THIS club's recruitments,
    or None. Anything else is ``invalid_application`` — never a hint about whose
    application the id was.
    """
    if application_id in (None, ""):
        return None

    application = (
        RecruitmentApplication.objects
        .select_related("recruitment")
        .filter(
            id=application_id,
            recruitment__organization=organization,
            applicant=player,
            status=RecruitmentApplication.Status.SELECTED,
        )
        .first()
        if is_valid_uuid(str(application_id)) else None
    )
    if application is None:
        raise SquadError(
            "invalid_application",
            "This application can't be used for this invite.",
            field="application_id",
        )
    return application


def _live_row(squad, user):
    """
    The player's live row in this squad (locked), or None. An expired invite is
    deleted here and reads as None — this is where read-time expiry frees the
    partial unique constraint.
    """
    row = (
        SquadMember.objects
        .select_for_update()
        .filter(squad=squad, user=user, status__in=LIVE_MEMBER_STATUSES)
        .first()
    )
    if row is not None and row.status == Status.INVITED and not _is_open_invite(row):
        row.delete()
        return None
    return row


def _squad_row(squad, member_id):
    """A row of this squad, any status, locked — or the 404."""
    row = (
        SquadMember.objects
        .select_for_update()
        .filter(id=member_id, squad=squad)
        .first()
        if is_valid_uuid(str(member_id)) else None
    )
    if row is None:
        raise member_not_found()
    return row


def _pending_row(squad, member_id):
    row = (
        SquadMember.objects
        .select_for_update()
        .filter(id=member_id, squad=squad, status=Status.PENDING)
        .first()
        if is_valid_uuid(str(member_id)) else None
    )
    if row is None:
        raise SquadError("not_pending", NOT_PENDING_MESSAGE)
    return row


def _active_row(squad, member_id):
    row = _squad_row(squad, member_id)
    if row.status != Status.ACTIVE:
        raise SquadError("not_active", NOT_ACTIVE_MESSAGE)
    return row


def _lock_live_squad(squad_id):
    """The squad behind a player's own row, locked, or None if it was deleted."""
    return (
        Squad.objects
        .select_for_update(of=("self",))
        .select_related("sport", "organization")
        .filter(id=squad_id, is_deleted=False)
        .first()
    )


def _own_row(user, member_id):
    if not is_valid_uuid(str(member_id)):
        return None
    return SquadMember.objects.filter(id=member_id, user=user).first()


def _check_player_for_self(user, organization, message):
    """
    check_player on the player's own path. A refusal that would read "Couldn't
    add this player" to the club reads as ``message`` here — the same sentence
    a closed link or invite gives, so a block is never told apart from it.
    """
    try:
        check_player(user, organization)
    except SquadError as exc:
        if exc.code == "cannot_add":
            raise SquadError("invite_closed", message)
        raise


def _require_room(squad):
    if _count(squad, status=Status.ACTIVE) >= MAX_ACTIVE_PER_SQUAD:
        raise SquadError("member_limit", SQUAD_FULL_MESSAGE)


# =====================================================================
# THE SERVICE
# =====================================================================

class MembershipService:

    # -----------------------------------------------------------------
    # THE CAREER ENTRY RULES
    # -----------------------------------------------------------------
    @staticmethod
    def _attach_career_entry(membership, verifier_user):
        """
        Back an ACTIVE membership with a VERIFIED career entry at the club.

        Written on the model directly, NOT through CareerEntryService: its
        "material edit → pending" rule is for the owner's own edits, and its
        per-user cap is for the owner's own claims — a club-made record of a
        real squad place bypasses both. No career_verified notification: the
        squad notification is the one that says it.
        """
        squad = membership.squad
        now = timezone.now()

        entry = (
            CareerEntry.objects
            .select_for_update()
            .filter(
                user_id=membership.user_id,
                organization_id=squad.organization_id,
                sport_id=squad.sport_id,
                is_current=True,
            )
            .exclude(verification_status=CareerEntry.VerificationStatus.REJECTED)
            .order_by("-created_at")
            .first()
        )

        if entry is not None:
            membership.career_entry_prior_status = entry.verification_status
            membership.career_entry_created = False

            entry.verification_status = CareerEntry.VerificationStatus.VERIFIED
            entry.verified_by = verifier_user
            entry.verified_at = now
            fields = ["verification_status", "verified_by", "verified_at", "updated_at"]
            if squad.level:
                entry.squad_level = squad.level
                fields.append("squad_level")
            if squad.age_group:
                entry.age_group = squad.age_group
                fields.append("age_group")
            entry.save(update_fields=fields)
        else:
            # The pipeline's own entry may already hold this application (a
            # unique constraint): then the new entry simply does not cite it.
            application_id = membership.recruitment_application_id
            if application_id and CareerEntry.objects.filter(
                recruitment_application_id=application_id
            ).exists():
                application_id = None

            organization = squad.organization
            entry = CareerEntry.objects.create(
                user_id=membership.user_id,
                organization=organization,
                sport_id=squad.sport_id,
                title="Player",
                entry_type=ENTRY_TYPE_BY_ORG_TYPE.get(
                    organization.type, CareerEntry.EntryType.CLUB_TEAM
                ),
                squad_level=squad.level,
                age_group=squad.age_group,
                start_date=_today(),
                is_current=True,
                verification_status=CareerEntry.VerificationStatus.VERIFIED,
                verified_by=verifier_user,
                verified_at=now,
                source=CareerEntry.Source.SQUAD,
                recruitment_application_id=application_id,
            )
            membership.career_entry_prior_status = ""
            membership.career_entry_created = True

        if membership.position_id:
            entry.positions.add(membership.position_id)

        membership.career_entry = entry
        membership.save(
            update_fields=[
                "career_entry",
                "career_entry_created",
                "career_entry_prior_status",
                "updated_at",
            ]
        )
        return entry

    @staticmethod
    def _release_career_entry(membership, how):
        """
        What ending ``membership`` does to its career entry.

        ``how="close"`` (left, removed as "left", squad deleted, account
        deleted): the stint ends today and stays VERIFIED — the time at the club
        still counts. ``how="undo"`` ("added by mistake"): the entry goes back
        to what it was before this membership — deleted if this membership made
        it, else its old status restored.

        Nothing happens while another ACTIVE membership (any squad of the club)
        still links the same entry: the player is still at the club.
        """
        entry_id = membership.career_entry_id
        if not entry_id:
            return

        still_at_club = (
            SquadMember.objects
            .filter(career_entry_id=entry_id, status=Status.ACTIVE)
            .exclude(id=membership.id)
            .exists()
        )
        if still_at_club:
            return

        entry = CareerEntry.objects.select_for_update().filter(id=entry_id).first()
        if entry is None:
            return

        if how == "close":
            # A stint the player already ended themselves keeps their date.
            if not entry.is_current:
                return
            entry.end_date = max(_today(), entry.start_date)
            entry.is_current = False
            entry.save(update_fields=["end_date", "is_current", "updated_at"])
            return

        if how != "undo":
            raise ValueError(f"Unknown release: {how!r}")

        if membership.career_entry_created:
            # SET_NULL on every membership row that linked it, history too.
            entry.delete()
            return

        prior = membership.career_entry_prior_status
        if not prior:
            return

        entry.verification_status = prior
        fields = ["verification_status", "updated_at"]
        if prior != CareerEntry.VerificationStatus.VERIFIED:
            entry.verified_by = None
            entry.verified_at = None
            fields += ["verified_by", "verified_at"]
        entry.save(update_fields=fields)

    @staticmethod
    def recount(squad):
        """Both counters from the rows. The bulk paths and account deletion end here."""
        Squad.objects.filter(id=squad.id).update(
            members_count=_count(squad, status=Status.ACTIVE),
            pending_count=_count(squad, status=Status.PENDING),
        )
        bust_public_squads(squad.organization_id)

    # -----------------------------------------------------------------
    # SHARED TRANSITIONS (the caller holds the squad lock and the row lock)
    # -----------------------------------------------------------------
    @staticmethod
    def _approve_row(squad, row, approver, *, position=None, jersey=None,
                     is_captain=False):
        """
        PENDING → ACTIVE, as the club. ``position`` None means "not given": the
        player's own primary position for the sport is used.
        """
        check_player(row.user, squad.organization)
        _require_room(squad)

        if position is None:
            position = _primary_position(row.user, squad.sport_id)

        if _jersey_taken(squad, jersey, exclude_id=row.id):
            raise SquadError("jersey_taken", JERSEY_TAKEN_MESSAGE, field="jersey_number")

        if is_captain:
            _clear_captain(squad, exclude_id=row.id)

        row.status = Status.ACTIVE
        row.position = position
        row.jersey_number = jersey
        row.is_captain = is_captain
        row.joined_at = _today()
        row.approved_by = approver
        row.approved_at = timezone.now()
        row.expires_at = None
        row.save(
            update_fields=[
                "status", "position", "jersey_number", "is_captain", "joined_at",
                "approved_by", "approved_at", "expires_at", "updated_at",
            ]
        )
        _bump(squad, members=1, pending=-1)

        MembershipService._attach_career_entry(row, approver.user)
        _after_commit(NotificationService.squad_request_approved, row)

        return row

    @staticmethod
    def _activate_invite(squad, row):
        """
        INVITED → ACTIVE, as the player.

        An invite carries its sender's authority, so it ends with it: the
        inviter must still be an active member holding squads.review (the same
        idea as the org's ``_inviter_still_stands``). Their row is locked so a
        removal racing this accept is settled one way or the other.
        """
        if not _is_open_invite(row):
            raise invite_closed()

        inviter = (
            OrganizationMember.all_objects
            .select_for_update(of=("self",))
            .select_related("user")
            .filter(id=row.invited_by_member_id)
            .first()
            if row.invited_by_member_id else None
        )
        if (
            inviter is None
            or inviter.organization_id != squad.organization_id
            or inviter.status != OrganizationMember.Status.ACTIVE
            or not inviter.user.is_active
            or not has_org_permission(inviter, REVIEW)
        ):
            raise invite_closed()

        _require_room(squad)

        # The number was free when the club sent the invite; somebody may have
        # taken it since. The place matters more than the number.
        if _jersey_taken(squad, row.jersey_number, exclude_id=row.id):
            row.jersey_number = None

        row.status = Status.ACTIVE
        row.joined_at = _today()
        row.approved_by = inviter
        row.approved_at = timezone.now()
        row.expires_at = None
        row.save(
            update_fields=[
                "status", "jersey_number", "joined_at", "approved_by",
                "approved_at", "expires_at", "updated_at",
            ]
        )
        _bump(squad, members=1)

        MembershipService._attach_career_entry(row, inviter.user)
        _after_commit(NotificationService.squad_invite_accepted, row, inviter.user)

        return row

    @staticmethod
    def _end_active(squad, row, *, status, ended_by, how):
        """ACTIVE → LEFT / REMOVED / MOVED, with the career entry released by ``how``."""
        row.status = status
        row.left_at = _today()
        row.ended_by = ended_by
        row.is_captain = False
        row.save(
            update_fields=["status", "left_at", "ended_by", "is_captain", "updated_at"]
        )
        if how:
            MembershipService._release_career_entry(row, how)

    # -----------------------------------------------------------------
    # 1. ASKING THROUGH THE LINK (the player)
    # -----------------------------------------------------------------
    @staticmethod
    def request_join(user, squad):
        """
        ``(membership, created)``. Asking twice is the same request; a player
        with an open invite who taps the link is saying yes to the invite.
        """
        with transaction.atomic():
            squad = _lock_live_squad(squad.id)
            if squad is None:
                raise squad_not_found()

            organization = squad.organization
            if not squad.invite_enabled or not _org_is_usable(organization):
                raise SquadError("invite_closed", LINK_CLOSED_MESSAGE)

            _check_player_for_self(user, organization, LINK_CLOSED_MESSAGE)

            row = _live_row(squad, user)
            if row is not None:
                if row.status == Status.PENDING:
                    return row, False

                if row.status == Status.ACTIVE:
                    raise SquadError("already_member", "You're already in this squad.")

                # An open invite. If its sender can no longer stand behind it,
                # it goes, and the tap becomes an ordinary request.
                try:
                    with transaction.atomic():
                        return MembershipService._activate_invite(squad, row), False
                except SquadError as exc:
                    if exc.code != "invite_closed":
                        raise
                    row.delete()

            if _count(squad, status=Status.PENDING) >= MAX_PENDING_PER_SQUAD:
                raise SquadError(
                    "request_limit", "This squad isn't taking requests right now."
                )

            row = SquadMember.objects.create(
                squad=squad,
                user=user,
                status=Status.PENDING,
                source=Source.LINK,
            )
            _bump(squad, pending=1)
            _after_commit(NotificationService.squad_join_request, row)

        logger.info("[SQUAD] join requested | squad=%s | member=%s", squad.id, row.id)
        return row, True

    # -----------------------------------------------------------------
    # 2–3. DIRECT INVITES (the club)
    # -----------------------------------------------------------------
    @staticmethod
    def invite(actor, squad_id, user_id, position_id=None, jersey_number=None,
               application_id=None):
        """
        An INVITED row the player accepts — or, when they already asked through
        the link, their request approved on the spot (returned ACTIVE).
        """
        organization, member = require_squads_permission(actor, REVIEW)

        player = (
            User.objects.select_related("profile").filter(id=user_id).first()
            if is_valid_uuid(str(user_id)) else None
        )
        check_player(player, organization)

        jersey = _clean_jersey(jersey_number)
        application = _clean_application(organization, player, application_id)

        with transaction.atomic():
            squad = get_org_squad(organization, squad_id, lock=True)
            member = relock_actor(member, REVIEW)
            position = _clean_position(squad, position_id)

            row = _live_row(squad, player)
            if row is not None:
                if row.status == Status.ACTIVE:
                    raise SquadError(
                        "already_member", "This player is already in the squad."
                    )
                if row.status == Status.INVITED:
                    raise SquadError(
                        "already_invited", "This player already has an invite to this squad."
                    )

                # PENDING: they already asked. Inviting them is saying yes.
                if application is not None:
                    row.recruitment_application = application
                    row.save(update_fields=["recruitment_application", "updated_at"])
                return MembershipService._approve_row(
                    squad, row, member, position=position, jersey=jersey
                )

            if (
                SquadMember.objects
                .filter(_open_invites_q(), squad=squad)
                .count() >= MAX_OPEN_INVITES_PER_SQUAD
            ):
                raise SquadError(
                    "invite_limit",
                    f"A squad can have up to {MAX_OPEN_INVITES_PER_SQUAD} open "
                    f"invites. Cancel one to send another.",
                )

            _require_room(squad)

            if _jersey_taken(squad, jersey):
                raise SquadError(
                    "jersey_taken", JERSEY_TAKEN_MESSAGE, field="jersey_number"
                )

            row = SquadMember.objects.create(
                squad=squad,
                user=player,
                status=Status.INVITED,
                source=Source.RECRUITMENT if application else Source.INVITE,
                invited_by_member=member,
                recruitment_application=application,
                position=position,
                jersey_number=jersey,
                expires_at=timezone.now() + INVITE_TTL,
            )
            _after_commit(NotificationService.squad_invite, row)

        logger.info("[SQUAD] invite sent | squad=%s | member=%s", squad.id, row.id)
        return row

    @staticmethod
    def cancel_invite(actor, squad_id, member_id):
        organization, member = require_squads_permission(actor, REVIEW)

        with transaction.atomic():
            squad = get_org_squad(organization, squad_id, lock=True)
            relock_actor(member, REVIEW)

            row = _squad_row(squad, member_id)
            if row.status != Status.INVITED:
                raise SquadError("not_pending", "This invite is no longer pending.")
            row.delete()

    # -----------------------------------------------------------------
    # 4–5. ANSWERING LINK REQUESTS (the club)
    # -----------------------------------------------------------------
    @staticmethod
    def approve(actor, squad_id, items):
        """
        ``{approved: [membership], failed: [{member_id, code, message}]}``.

        Each item stands alone: one bad item never stops the rest (a savepoint
        each), and the squad's counters are recounted once at the end.
        """
        organization, member = require_squads_permission(actor, REVIEW)
        items = _clean_batch(items, "items")

        approved, failed = [], []

        with transaction.atomic():
            squad = get_org_squad(organization, squad_id, lock=True)
            member = relock_actor(member, REVIEW)

            for item in items:
                item = item if isinstance(item, dict) else {}
                member_id = item.get("member_id")

                try:
                    with transaction.atomic():
                        row = _pending_row(squad, member_id)
                        position = _clean_position(squad, item.get("position_id"))
                        jersey = _clean_jersey(item.get("jersey_number"))
                        is_captain = _clean_bool(
                            item.get("is_captain", False), "is_captain"
                        )
                        approved.append(
                            MembershipService._approve_row(
                                squad, row, member,
                                position=position, jersey=jersey,
                                is_captain=is_captain,
                            )
                        )
                except SquadError as exc:
                    failed.append(_failure(member_id, exc.code, exc.message))
                except IntegrityError:
                    failed.append(_failure(member_id, "cannot_add", CANNOT_ADD_MESSAGE))

            MembershipService.recount(squad)

        return {"approved": approved, "failed": failed}

    @staticmethod
    def decline(actor, squad_id, member_ids):
        """``{declined: [ids], failed: [...]}``. Declined requests are deleted."""
        organization, member = require_squads_permission(actor, REVIEW)
        member_ids = _clean_batch(member_ids, "member_ids")

        declined, failed = [], []

        with transaction.atomic():
            squad = get_org_squad(organization, squad_id, lock=True)
            relock_actor(member, REVIEW)

            for member_id in member_ids:
                try:
                    row = _pending_row(squad, member_id)
                except SquadError as exc:
                    failed.append(_failure(member_id, exc.code, exc.message))
                    continue

                player = row.user
                row.delete()
                declined.append(str(member_id))
                _after_commit(NotificationService.squad_request_declined, squad, player)

            MembershipService.recount(squad)

        return {"declined": declined, "failed": failed}

    # -----------------------------------------------------------------
    # 6–8. MANAGING ACTIVE PLAYERS (the club)
    # -----------------------------------------------------------------
    @staticmethod
    def update_member(actor, squad_id, member_id, payload):
        """Partial: position_id, jersey_number, is_captain, notes."""
        organization, member = require_squads_permission(actor, REVIEW)
        payload = payload if isinstance(payload, dict) else {}

        with transaction.atomic():
            squad = get_org_squad(organization, squad_id, lock=True)
            relock_actor(member, REVIEW)
            row = _active_row(squad, member_id)

            fields = ["updated_at"]

            if "position_id" in payload:
                position = _clean_position(squad, payload["position_id"])
                row.position = position
                fields.append("position")
                if position is not None and row.career_entry_id:
                    row.career_entry.positions.add(position)

            if "jersey_number" in payload:
                jersey = _clean_jersey(payload["jersey_number"])
                if _jersey_taken(squad, jersey, exclude_id=row.id):
                    raise SquadError(
                        "jersey_taken", JERSEY_TAKEN_MESSAGE, field="jersey_number"
                    )
                row.jersey_number = jersey
                fields.append("jersey_number")

            if "is_captain" in payload:
                is_captain = _clean_bool(payload["is_captain"], "is_captain")
                if is_captain:
                    _clear_captain(squad, exclude_id=row.id)
                row.is_captain = is_captain
                fields.append("is_captain")

            if "notes" in payload:
                notes = payload["notes"]
                if notes is None:
                    notes = ""
                if not isinstance(notes, str):
                    raise SquadError("invalid_field", "Notes must be text.", field="notes")
                notes = notes.strip()
                if len(notes) > NOTES_MAX:
                    raise SquadError(
                        "invalid_field",
                        f"Notes can be up to {NOTES_MAX} characters.",
                        field="notes",
                    )
                row.notes = notes
                fields.append("notes")

            row.save(update_fields=fields)
            bust_public_squads(squad.organization_id)

        return row

    @staticmethod
    def remove(actor, squad_id, member_id, reason):
        """
        ``reason="left"`` → LEFT, the career stint closed (it still counts).
        ``reason="mistake"`` → REMOVED, the career entry undone.
        """
        organization, member = require_squads_permission(actor, REVIEW)

        releases = {"left": (Status.LEFT, "close"), "mistake": (Status.REMOVED, "undo")}
        if reason not in releases:
            raise SquadError(
                "invalid_reason",
                "Choose whether the player left the squad or was added by mistake.",
                field="reason",
            )
        status, how = releases[reason]

        with transaction.atomic():
            squad = get_org_squad(organization, squad_id, lock=True)
            member = relock_actor(member, REVIEW)
            row = _active_row(squad, member_id)

            MembershipService._end_active(
                squad, row, status=status, ended_by=member, how=how
            )
            _bump(squad, members=-1)
            _after_commit(NotificationService.squad_member_removed, squad, row.user)

        logger.info(
            "[SQUAD] member removed | squad=%s | member=%s | reason=%s",
            squad.id, row.id, reason,
        )

    @staticmethod
    def move(actor, squad_id, member_id, to_squad_id):
        """
        To another squad of the same club and sport. The old row is kept as
        MOVED; the new one carries the same career entry, so nothing is
        released — the player never left the club.
        """
        organization, member = require_squads_permission(actor, REVIEW)

        if not is_valid_uuid(str(to_squad_id)):
            raise SquadError("not_found", "That squad wasn't found.", status_code=404)
        if str(to_squad_id) == str(squad_id):
            raise SquadError("same_squad", "The player is already in this squad.")

        with transaction.atomic():
            # Both squads, in id order, so two moves in opposite directions
            # cannot deadlock.
            locked = {}
            for an_id in sorted({str(squad_id), str(to_squad_id)}):
                locked[an_id] = get_org_squad(organization, an_id, lock=True)
            squad = locked[str(squad_id)]
            target = locked[str(to_squad_id)]

            member = relock_actor(member, REVIEW)

            if target.sport_id != squad.sport_id:
                raise SquadError(
                    "sport_mismatch",
                    "A player can only move to a squad for the same sport.",
                )

            row = _active_row(squad, member_id)
            player = row.user

            existing = _live_row(target, player)
            if existing is not None:
                raise SquadError(*ALREADY_IN_TARGET[existing.status])

            _require_room(target)

            MembershipService._end_active(
                squad, row, status=Status.MOVED, ended_by=member, how=None
            )

            jersey = row.jersey_number
            if _jersey_taken(target, jersey):
                jersey = None

            moved = SquadMember.objects.create(
                squad=target,
                user=player,
                status=Status.ACTIVE,
                source=row.source,
                invited_by_member=row.invited_by_member,
                recruitment_application=row.recruitment_application,
                career_entry=row.career_entry,
                career_entry_created=row.career_entry_created,
                career_entry_prior_status=row.career_entry_prior_status,
                position=row.position,
                jersey_number=jersey,
                # Same club: the club's own notes about the player go with them.
                notes=row.notes,
                joined_at=_today(),
                approved_by=member,
                approved_at=timezone.now(),
            )

            # The stint now describes the squad they are in: the target's level
            # and age group, VERBATIM, blank included. Keeping the old value
            # when the target's is blank made a move from U17 into a plain
            # senior squad read "Senior · U17".
            if moved.career_entry_id:
                entry = (
                    CareerEntry.objects.select_for_update()
                    .filter(id=moved.career_entry_id).first()
                )
                if entry is not None and (
                    entry.squad_level != target.level or entry.age_group != target.age_group
                ):
                    entry.squad_level = target.level
                    entry.age_group = target.age_group
                    entry.save(update_fields=["squad_level", "age_group", "updated_at"])

            _bump(squad, members=-1)
            _bump(target, members=1)
            _after_commit(NotificationService.squad_member_moved, squad, target, player)

        logger.info(
            "[SQUAD] member moved | from=%s | to=%s | member=%s",
            squad.id, target.id, moved.id,
        )
        return moved

    # -----------------------------------------------------------------
    # 9–12. THE PLAYER'S OWN ANSWERS
    # -----------------------------------------------------------------
    @staticmethod
    def accept_invite(user, member_id):
        row = _own_row(user, member_id)
        if row is None or row.status != Status.INVITED:
            raise invite_closed()

        with transaction.atomic():
            squad = _lock_live_squad(row.squad_id)
            if squad is None or not _org_is_usable(squad.organization):
                raise invite_closed()

            row = (
                SquadMember.objects.select_for_update()
                .filter(id=row.id, user=user, status=Status.INVITED)
                .first()
            )
            if row is None:
                raise invite_closed()

            _check_player_for_self(user, squad.organization, invite_closed().message)
            row = MembershipService._activate_invite(squad, row)

        logger.info("[SQUAD] invite accepted | squad=%s | member=%s", squad.id, row.id)
        return row

    @staticmethod
    def decline_invite(user, member_id):
        """Declined invites are deleted, and nobody is told."""
        row = _own_row(user, member_id)
        if row is None or row.status != Status.INVITED:
            raise invite_closed()

        with transaction.atomic():
            _lock_live_squad(row.squad_id)
            deleted, _ = SquadMember.objects.filter(
                id=row.id, user=user, status=Status.INVITED
            ).delete()
            if not deleted:
                raise invite_closed()

    @staticmethod
    def withdraw_request(user, member_id):
        row = _own_row(user, member_id)
        if row is None or row.status != Status.PENDING:
            raise SquadError("not_pending", NOT_PENDING_MESSAGE)

        with transaction.atomic():
            squad = _lock_live_squad(row.squad_id)
            deleted, _ = SquadMember.objects.filter(
                id=row.id, user=user, status=Status.PENDING
            ).delete()
            if not deleted:
                raise SquadError("not_pending", NOT_PENDING_MESSAGE)
            if squad is not None:
                _bump(squad, pending=-1)

    @staticmethod
    def leave(user, member_id):
        row = _own_row(user, member_id)
        if row is None or row.status != Status.ACTIVE:
            raise SquadError("not_active", "You're not in this squad any more.")

        with transaction.atomic():
            squad = _lock_live_squad(row.squad_id)
            row = (
                SquadMember.objects.select_for_update()
                .filter(id=row.id, user=user, status=Status.ACTIVE)
                .first()
            )
            if squad is None or row is None:
                raise SquadError("not_active", "You're not in this squad any more.")

            MembershipService._end_active(
                squad, row, status=Status.LEFT, ended_by=None, how="close"
            )
            _bump(squad, members=-1)

        logger.info("[SQUAD] member left | squad=%s | member=%s", squad.id, row.id)

    # -----------------------------------------------------------------
    # 13. VERIFYING A CAREER CLAIM INTO A SQUAD (Stage 3's verify flow)
    # -----------------------------------------------------------------
    @staticmethod
    def add_verified_entry_to_squad(acting_member, squad, entry, prior_status,
                                    position_id=None, jersey_number=None):
        """
        The club verified ``entry`` and put its player in ``squad``. Called inside
        the career-verify transaction, which already checked the permission and
        set the entry VERIFIED; this only links it. ``prior_status`` is the
        entry's status before that verify, kept so "added by mistake" can put
        it back.
        """
        jersey = _clean_jersey(jersey_number)

        with transaction.atomic():
            squad = _lock_live_squad(squad.id)
            if squad is None or squad.organization_id != acting_member.organization_id:
                raise squad_not_found()
            if entry.organization_id != squad.organization_id:
                raise squad_not_found()
            if entry.sport_id != squad.sport_id:
                raise SquadError(
                    "sport_mismatch",
                    "This career entry is for a different sport than the squad.",
                )

            player = entry.user
            check_player(player, squad.organization)
            position = _clean_position(squad, position_id)

            row = _live_row(squad, player)
            if row is not None and row.status == Status.ACTIVE:
                raise SquadError("already_member", "This player is already in the squad.")

            _require_room(squad)

            if jersey is None and row is not None:
                jersey = row.jersey_number
                if _jersey_taken(squad, jersey, exclude_id=row.id):
                    jersey = None
            elif _jersey_taken(squad, jersey, exclude_id=row.id if row else None):
                raise SquadError(
                    "jersey_taken", JERSEY_TAKEN_MESSAGE, field="jersey_number"
                )

            if position is None:
                position = (
                    row.position if row is not None and row.position_id
                    else _primary_position(player, squad.sport_id)
                )

            now = timezone.now()

            if row is not None:
                was_pending = row.status == Status.PENDING
                row.status = Status.ACTIVE
                row.career_entry = entry
                row.career_entry_created = False
                row.career_entry_prior_status = prior_status or ""
                row.position = position
                row.jersey_number = jersey
                row.joined_at = _today()
                row.approved_by = acting_member
                row.approved_at = now
                row.expires_at = None
                row.save(
                    update_fields=[
                        "status", "career_entry", "career_entry_created",
                        "career_entry_prior_status", "position", "jersey_number",
                        "joined_at", "approved_by", "approved_at", "expires_at",
                        "updated_at",
                    ]
                )
                _bump(squad, members=1, pending=-1 if was_pending else 0)
            else:
                row = SquadMember.objects.create(
                    squad=squad,
                    user=player,
                    status=Status.ACTIVE,
                    source=Source.VERIFICATION,
                    career_entry=entry,
                    career_entry_created=False,
                    career_entry_prior_status=prior_status or "",
                    position=position,
                    jersey_number=jersey,
                    joined_at=_today(),
                    approved_by=acting_member,
                    approved_at=now,
                )
                _bump(squad, members=1)

            if position is not None:
                entry.positions.add(position)

        return row

    # -----------------------------------------------------------------
    # 14. ACCOUNT DELETION (Stage 3 wires it)
    # -----------------------------------------------------------------
    @staticmethod
    def end_all_for_user(user):
        """
        Every live row of ``user``: ACTIVE → LEFT with the stint closed,
        PENDING / INVITED deleted, then every touched squad recounted.
        """
        with transaction.atomic():
            squad_ids = sorted(
                {
                    str(squad_id)
                    for squad_id in SquadMember.objects
                    .filter(user=user, status__in=LIVE_MEMBER_STATUSES)
                    .values_list("squad_id", flat=True)
                }
            )
            squads = [
                squad for squad in (
                    Squad.objects.select_for_update().filter(id=squad_id).first()
                    for squad_id in squad_ids
                )
                if squad is not None
            ]

            rows = list(
                SquadMember.objects.select_for_update(of=("self",))
                .select_related("squad")
                .filter(user=user, status__in=LIVE_MEMBER_STATUSES)
            )
            for row in rows:
                if row.status == Status.ACTIVE:
                    MembershipService._end_active(
                        row.squad, row, status=Status.LEFT, ended_by=None, how="close"
                    )
                else:
                    row.delete()

            for squad in squads:
                MembershipService.recount(squad)

        logger.info(
            "[SQUAD] all memberships ended | user=%s | squads=%s",
            user.id, len(squads),
        )

    # -----------------------------------------------------------------
    # 15. WHO THE CLUB CAN INVITE
    # -----------------------------------------------------------------
    @staticmethod
    def candidates(actor, squad_id, q):
        """
        Up to 20 players matching ``q`` (name or username, ≥ 2 characters), each
        with their state in THIS squad: none | active | pending | invited.

        Anyone in a block with the club is left out, and so is an account a
        parent has not approved: check_player would refuse both, and offering a
        name that can only fail is worse than not offering it.
        """
        organization, _ = require_squads_permission(actor, REVIEW)
        squad = get_org_squad(organization, squad_id)

        q = (q or "").strip()[:CANDIDATE_MAX_QUERY]
        if len(q) < CANDIDATE_MIN_QUERY:
            return []

        users = (
            User.objects
            .filter(
                role=User.Role.PLAYER,
                is_active=True,
                deletion_requested_at__isnull=True,
                username__isnull=False,
            )
            .exclude(username="")
            .exclude(guardian_consent_status__in=BLOCKING_STATUSES)
            .filter(Q(username__icontains=q) | Q(profile__name__icontains=q))
            .select_related("profile")
            .order_by("username")
        )

        blocked_user_ids, _ = blocked_id_sets(actor)
        if blocked_user_ids:
            users = users.exclude(id__in=blocked_user_ids)

        users = list(users[:CANDIDATES_LIMIT])
        if not users:
            return []

        user_ids = [user.id for user in users]
        now = timezone.now()

        states = {}
        for row in SquadMember.objects.filter(
            squad=squad, user_id__in=user_ids, status__in=LIVE_MEMBER_STATUSES
        ):
            if row.status == Status.INVITED and not _is_open_invite(row, now):
                continue
            states[row.user_id] = row.status

        primaries = primary_positions_for(user_ids, squad.sport_id)

        return [
            {
                "user": user,
                "primary_position": primaries.get(user.id),
                "state": states.get(user.id, "none"),
            }
            for user in users
        ]


def _failure(member_id, code, message):
    return {
        "member_id": str(member_id) if member_id else None,
        "code": code,
        "message": message,
    }
