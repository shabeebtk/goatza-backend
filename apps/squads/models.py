"""
Squads: an organization's standing groups of players ("U17 Boys", "Senior
Men", "Sprints Group"), each for ONE sport.

The word is "squad" in code and in the UI, because "team" is already an
organization type. Only users with role=player can be members.

THE CAREER RULE. Every ACTIVE membership is backed by a VERIFIED CareerEntry at
that club: one entry per player per club + sport, not one per squad, so a
player moved between two squads of the same club keeps one entry. The link is
``SquadMember.career_entry`` and it points FROM the membership TO the entry, so
deleting a squad (or a membership) can never cascade into a career entry.

What a sport needs — positions, jersey numbers, the words for a member, the
order of the roster sections — is seeded DATA (``SportSquadConfig`` and
``SportPosition.group``, see ``seed_squad_configs``), never code.
"""

import secrets

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower

from apps.accounts.models import User
from apps.organization.models import Organization, OrganizationMember
from apps.sports.models import Sport, SportPosition
from shared.models import BaseUUIDModel


def new_invite_token() -> str:
    """A squad's shareable invite-link token. Also what a reset writes."""
    return secrets.token_urlsafe(24)


# The statuses that hold a player's place in a squad. At most one such row per
# (squad, user); LEFT / REMOVED / MOVED rows are history and may repeat.
LIVE_MEMBER_STATUSES = ["pending", "invited", "active"]


def validate_group_order(value):
    """
    A list of distinct, non-empty section names. The field is edited by hand
    in the admin, and a typo'd shape would otherwise surface as a broken
    roster rather than a form error.
    """
    if not isinstance(value, list):
        raise ValidationError("group_order must be a list of section names.")
    if not all(isinstance(name, str) and name.strip() for name in value):
        raise ValidationError("Every section name must be non-empty text.")
    if len(set(value)) != len(value):
        raise ValidationError("Section names must not repeat.")


class SportSquadConfig(BaseUUIDModel):
    """
    How squads behave for one sport — seeded catalog data, edited in the admin.

    A sport with NO row here still works: ``squad_config_for`` hands back the
    permissive default (positions on, jersey numbers off, "player"/"players",
    no section order), so a new sport needs no seed before its first squad.
    """

    sport = models.OneToOneField(
        Sport,
        on_delete=models.CASCADE,
        related_name="squad_config",
    )
    uses_positions = models.BooleanField(default=True)
    uses_jersey_numbers = models.BooleanField(default=False)
    member_noun = models.CharField(max_length=20, default="player")
    member_noun_plural = models.CharField(max_length=20, default="players")
    # Roster section order, by SportPosition.group:
    # ["Goalkeepers", "Defenders", "Midfielders", "Forwards"].
    group_order = models.JSONField(
        default=list,
        blank=True,
        validators=[validate_group_order],
    )
    # Display only ("Typical squad: 25"). NEVER a cap on members.
    typical_squad_size = models.PositiveSmallIntegerField(null=True, blank=True)

    class Meta:
        db_table = "sport_squad_configs"

    def __str__(self):
        return f"Squad config ({self.sport})"


class Squad(BaseUUIDModel):
    """
    One standing group of players at an organization, for one sport.

    Soft-deleted: ``is_deleted`` + ``deleted_at``. The name is unique per org
    among live squads, case-insensitively, so a deleted "U17" frees the name.

    ``invite_token`` is a SHAREABLE link, not a credential: it only lets a
    player ASK to join (a PENDING row the club approves). That is why it is
    stored raw, unlike an OrganizationInvite token — the club copies the link
    again whenever it likes. Reset = revoke: a new token kills the old link.

    ``members_count`` (ACTIVE rows) and ``pending_count`` (PENDING rows) are
    denormalized and kept by the services, never by hand.

    Deleting a squad must NEVER cascade into CareerEntry. It cannot: the link
    points from SquadMember to the entry, and a squad is soft-deleted anyway.
    """

    DESCRIPTION_MAX_LENGTH = 600  # enforced in the service

    # The same values as CareerEntry.SquadLevel, so the career entry a
    # membership creates copies this verbatim.
    class Level(models.TextChoices):
        YOUTH = "youth", "Youth"
        RESERVE = "reserve", "Reserve"
        SENIOR = "senior", "Senior"

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="squads",
    )
    sport = models.ForeignKey(
        Sport,
        on_delete=models.PROTECT,
        related_name="squads",
    )

    name = models.CharField(max_length=80)
    level = models.CharField(max_length=20, choices=Level.choices, blank=True)
    # Free text, like CareerEntry.age_group ("U17").
    age_group = models.CharField(max_length=20, blank=True)
    # Display only ("2026–27"). Never parsed.
    season = models.CharField(max_length=20, blank=True)
    description = models.TextField(blank=True)

    photo_url = models.URLField(max_length=500, blank=True)
    photo_public_id = models.CharField(max_length=300, blank=True)

    # "Show on club profile".
    is_public = models.BooleanField(default=True)

    invite_token = models.CharField(max_length=64, unique=True)
    invite_enabled = models.BooleanField(default=True)

    members_count = models.PositiveIntegerField(default=0)
    pending_count = models.PositiveIntegerField(default=0)

    created_by_member = models.ForeignKey(
        OrganizationMember,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_squads",
    )

    is_deleted = models.BooleanField(default=False, db_index=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "squads"
        ordering = ["created_at"]
        constraints = [
            models.UniqueConstraint(
                Lower("name"),
                "organization",
                condition=Q(is_deleted=False),
                name="unique_live_squad_name_per_org",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "is_deleted"]),
            models.Index(fields=["sport"]),
        ]

    def save(self, *args, **kwargs):
        if not self.invite_token:
            self.invite_token = new_invite_token()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.name} ({self.organization_id})"


class SquadMember(BaseUUIDModel):
    """
    One player's place in one squad, from the first ask or invite onwards.

    How a row starts:
      * PENDING — the player asked through the squad's invite link; the club
        approves (→ ACTIVE) or declines.
      * INVITED — the club invited a specific player (a Goatza player, or a
        Selected recruitment applicant); the player accepts (→ ACTIVE) or
        declines. Expires 14 days after it was sent (``expires_at``), read-time,
        like OrganizationInvite: no sweep.
      * ACTIVE directly — the club verified the player's pending career claim
        into the squad (source VERIFICATION).

    HISTORY RULE. LEFT / REMOVED / MOVED rows are KEPT: the player was in the
    squad, and their profile said so. Declined requests, withdrawn requests and
    declined / cancelled / expired invites are DELETED: nothing was ever on the
    player's profile, so there is nothing to keep.

    ``career_entry`` is SET_NULL: a player deleting their entry degrades this
    row, never deletes it. ``career_entry_created`` / ``career_entry_prior_status``
    record what this membership did to the entry, so ending it can put the
    entry back the way it found it.

    ``notes`` is club-private. NEVER serialize it to the player or the public.
    """

    INVITE_TTL_DAYS = 14
    NOTES_MAX_LENGTH = 500  # enforced in the service

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"    # asked through the link, waiting for the club
        INVITED = "invited", "Invited"    # the club invited them, waiting for the player
        ACTIVE = "active", "Active"
        LEFT = "left", "Left"             # player left, or the club removed them as "left the squad"
        REMOVED = "removed", "Removed"    # the club removed them as "added by mistake"
        MOVED = "moved", "Moved"          # moved to another squad of the same club

    class Source(models.TextChoices):
        LINK = "link", "Invite link"
        INVITE = "invite", "Direct invite"
        RECRUITMENT = "recruitment", "Recruitment"
        VERIFICATION = "verification", "Career verification"

    squad = models.ForeignKey(
        Squad,
        on_delete=models.CASCADE,
        related_name="memberships",
    )
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="squad_memberships",
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        db_index=True,
    )
    source = models.CharField(max_length=20, choices=Source.choices)

    invited_by_member = models.ForeignKey(
        OrganizationMember,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    recruitment_application = models.ForeignKey(
        "recruitments.RecruitmentApplication",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="squad_memberships",
    )

    career_entry = models.ForeignKey(
        "careers.CareerEntry",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="squad_memberships",
    )
    # True when THIS membership created the entry.
    career_entry_created = models.BooleanField(default=False)
    # The entry's verification_status before this membership verified it;
    # "" when this membership created it.
    career_entry_prior_status = models.CharField(max_length=20, blank=True)

    position = models.ForeignKey(
        SportPosition,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="squad_memberships",
    )
    jersey_number = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        validators=[MaxValueValidator(99)],
    )
    is_captain = models.BooleanField(default=False)

    notes = models.TextField(blank=True)

    requested_at = models.DateTimeField(auto_now_add=True, db_index=True)
    # INVITED only: requested_at + 14 days. Read-time expiry.
    expires_at = models.DateTimeField(null=True, blank=True)

    joined_at = models.DateField(null=True, blank=True)
    left_at = models.DateField(null=True, blank=True)

    approved_by = models.ForeignKey(
        OrganizationMember,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    ended_by = models.ForeignKey(
        OrganizationMember,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "squad_members"
        ordering = ["requested_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["squad", "user"],
                condition=Q(status__in=LIVE_MEMBER_STATUSES),
                name="unique_live_squad_member",
            ),
            models.UniqueConstraint(
                fields=["squad", "jersey_number"],
                condition=Q(status="active") & Q(jersey_number__isnull=False),
                name="unique_active_jersey_per_squad",
            ),
            models.UniqueConstraint(
                fields=["squad"],
                condition=Q(status="active") & Q(is_captain=True),
                name="unique_active_captain_per_squad",
            ),
            models.CheckConstraint(
                condition=Q(jersey_number__isnull=True) | Q(jersey_number__lte=99),
                name="squad_member_jersey_range",
            ),
        ]
        indexes = [
            models.Index(fields=["squad", "status"]),
            models.Index(fields=["user", "status"]),
        ]

    def __str__(self):
        return f"{self.user_id} in {self.squad_id} ({self.status})"


class SquadStaff(BaseUUIDModel):
    """
    A member of the organization credited on a squad: head coach, assistant
    coach, team manager, physio, or "other" with a free ``title``.

    ``member`` is an OrganizationMember, and member rows are never deleted —
    removal flips their status. So a staff row can outlive the person's access.
    THE RELATION-LOOKUP RULE (CLAUDE.md, Organization members): every read must
    filter ``member__status="active"`` in the SAME ``filter()`` call as the rest
    of the lookup. The service also checks the member belongs to the squad's
    organization; nothing here can.
    """

    class Role(models.TextChoices):
        HEAD_COACH = "head_coach", "Head coach"
        ASSISTANT_COACH = "assistant_coach", "Assistant coach"
        MANAGER = "manager", "Team manager"
        PHYSIO = "physio", "Physio"
        OTHER = "other", "Staff"

    squad = models.ForeignKey(
        Squad,
        on_delete=models.CASCADE,
        related_name="staff",
    )
    member = models.ForeignKey(
        OrganizationMember,
        on_delete=models.CASCADE,
        related_name="squad_staff_roles",
    )
    role = models.CharField(max_length=20, choices=Role.choices)
    # Shown instead of the role's label when role=other.
    title = models.CharField(max_length=40, blank=True)
    order = models.PositiveSmallIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "squad_staff"
        ordering = ["order", "created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["squad", "member"],
                name="unique_staff_per_squad",
            ),
        ]

    def __str__(self):
        return f"{self.member_id} on {self.squad_id} ({self.role})"
