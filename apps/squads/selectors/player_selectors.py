"""
Reads for the player's side: the invite link's page and "my squads".

THE INVITE LINK IS SHAREABLE, not a credential — it only lets a player ASK —
but its page still answers one generic 404 for every miss (unknown token, link
turned off, squad deleted, org unusable, a block), and it never names or shows
a single player.
"""

from django.db.models import Case, IntegerField, Q, Value, When
from django.utils import timezone

from apps.squads.models import LIVE_MEMBER_STATUSES, Squad, SquadMember, SquadStaff
from apps.squads.selectors.showcase_selectors import visible_staff

Status = SquadMember.Status

# Longer than any token we mint (secrets.token_urlsafe(24) is 32 characters)
# and short enough that a junk path never reaches the index.
MAX_TOKEN_LENGTH = 64


def squad_for_invite_token(token):
    """The squad behind a live, enabled invite link of a usable org, or None."""
    if not token or len(token) > MAX_TOKEN_LENGTH:
        return None

    return (
        Squad.objects
        .filter(
            invite_token=token,
            is_deleted=False,
            invite_enabled=True,
            organization__is_active=True,
            organization__is_suspended=False,
        )
        .select_related("sport", "organization", "organization__profile")
        .first()
    )


def head_coach_for(squad, actor):
    """
    The first head coach this caller may see, or None — under the showcase's
    staff rules (``visible_staff``): an active org member with a live account;
    for an anonymous caller, a public profile; for a signed-in one, nobody in a
    block with them.
    """
    return (
        visible_staff(actor)
        .filter(squad=squad, role=SquadStaff.Role.HEAD_COACH)
        .first()
    )


def live_row_for(squad, user):
    """This person's live row in the squad, or None. An expired invite reads as None."""
    row = SquadMember.objects.filter(
        squad=squad, user=user, status__in=LIVE_MEMBER_STATUSES
    ).first()
    if row is not None and row.status == Status.INVITED and (
        row.expires_at is None or row.expires_at <= timezone.now()
    ):
        return None
    return row


def my_squads(user):
    """
    The player's live rows: active, then open invites, then their requests —
    newest first inside each. Expired invites are left out, and so are invites
    and requests from an org that can no longer be joined.
    """
    now = timezone.now()
    usable_org = Q(
        squad__organization__is_active=True,
        squad__organization__is_suspended=False,
    )

    return list(
        SquadMember.objects
        .filter(user=user, squad__is_deleted=False)
        .filter(
            Q(status=Status.ACTIVE)
            | (Q(status=Status.INVITED, expires_at__gt=now) & usable_org)
            | (Q(status=Status.PENDING) & usable_org)
        )
        .select_related(
            "squad",
            "squad__sport",
            "squad__organization",
            "squad__organization__profile",
            "position",
        )
        .annotate(
            status_rank=Case(
                When(status=Status.ACTIVE, then=Value(0)),
                When(status=Status.INVITED, then=Value(1)),
                default=Value(2),
                output_field=IntegerField(),
            )
        )
        .order_by("status_rank", "-requested_at")
    )


def my_squad_row(user, member_id):
    """One of the player's rows joined for the "mine" item shape, or None."""
    return (
        SquadMember.objects
        .filter(id=member_id, user=user)
        .select_related(
            "squad",
            "squad__sport",
            "squad__organization",
            "squad__organization__profile",
            "position",
        )
        .first()
    )
