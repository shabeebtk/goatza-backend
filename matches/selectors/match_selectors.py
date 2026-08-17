"""
Read queries for the Match Diary.

There is NO visibility branch anywhere in this file, and that is the point:
in v1 every read is scoped to the owner, so the diary is private by
construction rather than by remembering to filter. ``MatchEntry.visibility``
exists in the schema and nothing here consults it.

When the diary opens up to followers, this is the one file that changes — which
is the whole reason the reads were kept in it.

Every list join is chosen so a page costs a constant number of queries: the
entries, their stats, and the stat catalog rows those stats point at. A diary
page of 20 matches with a dozen stats each must not become 250 queries because
a row learned to render a chip.
"""

from django.db.models import Prefetch, QuerySet

from matches.models import MatchEntry, MatchEntryStat, SportMatchStatField
from utils.validations import is_valid_uuid


def _stats_prefetch() -> Prefetch:
    """
    The entry's stats with their catalog row attached, in catalog order.

    Ordering here rather than in the caller means the compact diary row reads
    "G 2 · A 1 · SOT 4" in the order the admin arranged the sport's stats, not
    in whatever order the client happened to POST them.
    """
    return Prefetch(
        "stats",
        queryset=(
            MatchEntryStat.objects
            .select_related("stat_field")
            .order_by("stat_field__order", "stat_field__name")
        ),
    )


def owned_match(user, match_id):
    """
    One of ``user``'s live matches, fully joined for the detail screen, or None.

    Returns None — never raises — for a match that is missing, deleted, or
    somebody else's. That is the deliberate difference from
    ``MatchService._get_owned_match``, which separates 404 from 403 because a
    write needs to tell the player which it was. A read has nothing to gain
    from confirming that another player's match id exists.
    """
    if user is None or not is_valid_uuid(match_id):
        return None

    return (
        MatchEntry.objects
        .filter(id=match_id, user=user, is_deleted=False)
        .select_related("sport", "position", "career_entry")
        .prefetch_related(_stats_prefetch())
        .first()
    )


def list_matches(user, *, status=None, year=None, sport_id=None) -> QuerySet:
    """
    ``user``'s diary — newest match first, which is how a diary is read.

    Filters are all optional and all narrowing: ``status`` for the played /
    scheduled tabs, ``year`` for the season switcher, ``sport_id`` for a
    multi-sport player. ``year`` matches on the MATCH date, not on when the row
    was written, for the same reason the streak does.

    Four queries for any page size: the entries, the sport/position/career
    joins folded into that one, then the stats and their catalog rows.
    """
    if user is None:
        return MatchEntry.objects.none()

    queryset = (
        MatchEntry.objects
        .filter(user=user, is_deleted=False)
        .select_related("sport", "position", "career_entry")
        .prefetch_related(_stats_prefetch())
        .order_by("-date", "-created_at")
    )

    if status:
        queryset = queryset.filter(status=status)

    if year:
        queryset = queryset.filter(date__year=year)

    if sport_id:
        if not is_valid_uuid(sport_id):
            return MatchEntry.objects.none()
        queryset = queryset.filter(sport_id=sport_id)

    return queryset


def upcoming_matches(user, *, limit=None) -> QuerySet:
    """
    ``user``'s fixtures, NEXT ONE FIRST.

    Its own selector rather than a status filter on :func:`list_matches`
    because the ordering is the opposite: a diary reads backwards from today,
    a fixture list reads forwards. Sharing one function would mean a caller
    silently getting the furthest-away match at the top of "Up next".

    Deliberately NOT filtered by date. A fixture whose date has passed and that
    was never promoted is exactly what this list should keep showing — it is
    the prompt to go and log it, and hiding it is how a match gets lost.

    Matches with no kick-off time sort after timed ones on the same day
    (Postgres puts NULLs last on an ascending sort), which is the right way
    round: a known 3pm is more useful above a "sometime Saturday".
    """
    if user is None:
        return MatchEntry.objects.none()

    queryset = (
        MatchEntry.objects
        .filter(
            user=user,
            is_deleted=False,
            status=MatchEntry.Status.SCHEDULED,
        )
        .select_related("sport", "position", "career_entry")
        .prefetch_related(_stats_prefetch())
        .order_by("date", "kickoff_time")
    )

    if limit:
        return queryset[:limit]

    return queryset


def active_stat_fields(sport_id) -> QuerySet:
    """
    The stats a player can log for one sport — what the quick-add form is built
    from.

    Retired fields (``is_active=False``) are excluded, so a stat the admin
    stopped recording disappears from new forms while every value already
    logged against it stays readable.

    ``positions`` is prefetched even though v1 filters on nothing: the form
    ships the links so the client can start using them the moment the position
    rule lands, without this selector changing shape.
    """
    if not sport_id or not is_valid_uuid(sport_id):
        return SportMatchStatField.objects.none()

    return (
        SportMatchStatField.objects
        .filter(sport_id=sport_id, is_active=True)
        .prefetch_related("positions")
        .order_by("order", "name")
    )
