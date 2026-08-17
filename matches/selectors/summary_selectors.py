"""
The season summary — the number the diary exists to produce.

Everything here is aggregated in the database. A player with three seasons
logged is not a reason to pull a thousand rows into Python and add them up, and
the summary sits at the top of a screen people open constantly.

This is the one file in the app with a viewer branch, and it is deliberately
kept to ONE function. ``match_summary`` itself has no idea who is asking — it
takes a user and aggregates that user's matches. Whether a second person is
allowed to ask is decided once, in ``get_showcase_user``, so there is exactly
one place to look when the question is "who can see this".

Individual match entries stay owner-only regardless (see ``match_selectors``):
what a player opts into showing is the SUMMARY — the totals and the streak —
never the opponent names, notes and photos the entries themselves carry.

SPORT-AGNOSTIC, permanently. There is no branch on sport name in this module and
there must never be one — see :func:`match_summary` on ``zero_count`` for how
football's clean sheets are produced without a line of football in the backend.
"""

from django.db.models import Avg, Count, Q, Sum
from django.db.models.functions import Coalesce

from accounts.models import User
from matches.models import MatchDiarySettings, MatchEntry, MatchEntryStat

# How many matches the form chart shows.
FORM_LENGTH = 10


def get_showcase_user(username, viewer=None):
    """
    ``(user, settings)`` for a showcased diary summary, or None.

    Four separate reasons to return None, all of which the view reports as the
    same 404 — a visitor must not be able to tell a player who switched the
    showcase off from a deactivated account from a coach's username from a typo:

      * no such username
      * ``is_active`` False (deactivated / suspended)
      * the user is not a player
      * no settings row, or one with ``showcase_summary`` False

    ``User.username`` is nullable, so a user who never set one has no showcase
    URL at all. Nothing can match ``username=None`` through this lookup, but the
    guard is explicit because an empty-string username would otherwise resolve.

    Deliberately NOT routed through ``get_public_user``: that one also demands
    ``profile.is_public_profile``, which is the right gate for the CV because
    the CV is read by logged-out visitors. This surface is in-app and
    authenticated, and a player with a private profile still has teammates and
    coaches inside Goatza who should see the diary they chose to showcase.

    ``viewer`` short-circuits the showcase flag for the owner: previewing your
    own profile must not require switching a toggle on first.
    """
    if not username:
        return None

    user = (
        User.objects
        .filter(username=username, is_active=True)
        .first()
    )

    if user is None:
        return None

    if user.role != User.Role.PLAYER:
        return None

    settings = MatchDiarySettings.objects.filter(user=user).first()

    is_owner = viewer is not None and viewer.id == user.id

    if not is_owner and (settings is None or not settings.showcase_summary):
        return None

    # The owner may have no row yet — they have never opened the diary. Their
    # own preview reads the model defaults rather than 404ing, the same way
    # DiarySettingsService.get_or_create_for does, but WITHOUT writing a row:
    # somebody looking at their own profile is a read, not a first visit.
    if settings is None:
        settings = MatchDiarySettings(user=user)

    # Prime the FK cache with the user we already have.
    settings.user = user

    return user, settings


def _played_filters(user, year=None, sport_id=None) -> dict:
    """
    The one definition of "matches that count": this player's, played, not
    deleted, narrowed by the same optional season and sport the diary list uses.

    Returned as a dict of ORM lookups rather than a queryset so the stats
    aggregate below can apply it across the ``match_entry`` join — one grouped
    query instead of a queryset used as a subquery.
    """
    filters = {
        "user": user,
        "status": MatchEntry.Status.PLAYED,
        "is_deleted": False,
    }

    if year:
        filters["date__year"] = year

    if sport_id:
        filters["sport_id"] = sport_id

    return filters


def match_summary(user, *, year=None, sport_id=None) -> dict:
    """
    One player's totals over their played matches.

    Returns::

        {
          "total_matches", "wins", "losses", "draws",
          "minutes_total",        # nulls counted as 0
          "average_rating",       # over rated matches only; None if none are
          "form": [...],          # last 10 ratings, OLDEST FIRST, nulls kept
          "stats": [ {...}, ... ] # one row per stat actually logged
        }

    ``form`` keeps its nulls on purpose. A player who rated six of their last
    ten matches should see four gaps, not a six-long line pretending to be ten
    — the chart is a record of how they felt, and inventing the missing days
    would make it a lie. Oldest first so it reads left to right.

    Each ``stats`` row carries ``stat_field_id``, ``name``, ``short_label``,
    ``unit``, ``value_type``, ``total``, ``entries_count`` and ``zero_count``.
    Only stats the player has actually logged appear — an empty catalog row is
    not a zero, it is a stat they do not track.

    ``zero_count`` is the interesting one, and it is why this module can stay
    sport-agnostic. It counts the matches where the stat was logged AS ZERO,
    which is a different fact from never having logged it. The client reads it
    off "Goals conceded" and renders "7 clean sheets" — and off "Wickets" to
    say how often a bowler went wicketless — without the backend ever knowing
    what a clean sheet is. Any sport added later gets the same behaviour for
    free, because there is nothing sport-specific here to extend.

    Three queries regardless of how many matches are in range: the totals, the
    form list, and the grouped stats.
    """
    if user is None:
        return {
            "total_matches": 0,
            "wins": 0,
            "losses": 0,
            "draws": 0,
            "minutes_total": 0,
            "average_rating": None,
            "form": [],
            "stats": [],
        }

    filters = _played_filters(user, year, sport_id)
    played = MatchEntry.objects.filter(**filters)

    totals = played.aggregate(
        total_matches=Count("id"),
        wins=Count("id", filter=Q(result=MatchEntry.Result.WIN)),
        losses=Count("id", filter=Q(result=MatchEntry.Result.LOSS)),
        draws=Count("id", filter=Q(result=MatchEntry.Result.DRAW)),
        # A match logged without minutes counts as zero minutes, and a player
        # with no matches at all gets 0 rather than None.
        minutes_total=Coalesce(Sum("minutes_played"), 0),
        # Avg ignores NULLs, so this is already "over entries that have one",
        # and it returns None when none do.
        average_rating=Avg("self_rating"),
    )

    average_rating = totals["average_rating"]
    if average_rating is not None:
        # One scalar, rounded for display. 3.6666666666666665 in a JSON body
        # invites every client to round it differently.
        average_rating = round(float(average_rating), 2)

    # Newest first out of the DB (so the slice is the LAST ten), reversed in
    # Python for the chart. Ten values — this is not the summing this module
    # otherwise refuses to do in Python.
    recent_ratings = list(
        played
        .order_by("-date", "-created_at")
        .values_list("self_rating", flat=True)[:FORM_LENGTH]
    )

    stat_rows = (
        MatchEntryStat.objects
        .filter(**{f"match_entry__{key}": value for key, value in filters.items()})
        .values(
            "stat_field_id",
            "stat_field__name",
            "stat_field__short_label",
            "stat_field__unit",
            "stat_field__value_type",
            # Selected only so the ORDER BY below does not quietly widen the
            # GROUP BY behind our backs. Not returned.
            "stat_field__order",
        )
        .annotate(
            total=Sum("value"),
            entries_count=Count("id"),
            zero_count=Count("id", filter=Q(value=0)),
        )
        .order_by("stat_field__order", "stat_field__name")
    )

    return {
        "total_matches": totals["total_matches"],
        "wins": totals["wins"],
        "losses": totals["losses"],
        "draws": totals["draws"],
        "minutes_total": totals["minutes_total"],
        "average_rating": average_rating,
        "form": list(reversed(recent_ratings)),
        "stats": [
            {
                "stat_field_id": row["stat_field_id"],
                "name": row["stat_field__name"],
                "short_label": row["stat_field__short_label"],
                "unit": row["stat_field__unit"],
                "value_type": row["stat_field__value_type"],
                "total": row["total"],
                "entries_count": row["entries_count"],
                "zero_count": row["zero_count"],
            }
            for row in stat_rows
        ],
    }
