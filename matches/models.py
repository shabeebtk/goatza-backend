"""
The Match Diary — a player's own log of the matches they play.

The diary is the one place on Goatza where a player writes about themselves for
themselves. Career entries are claims an org verifies, achievements are awards
somebody gave them; a diary row is unverified by design and always will be. Its
value is that it exists at all — a season of matches nobody was recording.

Two things follow from that, and both shape the models here:

Logging has to be fast or it does not happen. ``SportMatchStatField.is_primary``
picks the three stats the quick-add form shows; everything else hides behind
"add more". A form that asks for twelve numbers is a form a player fills in
twice and then abandons.

Nothing here is public in v1. ``MatchEntry.visibility`` ships with all three
choices and a PRIVATE default, but no serializer accepts it and no selector
reads it — see the field's comment for why the column is here now rather than
in the migration that opens the diary up.

Stats are a per-sport catalog (``SportMatchStatField``), not columns, for the
same reason ``SportAttribute`` is: adding a sport is data entry, never a
deployment.
"""

from django.db import models
from django.db.models import Q

from accounts.models import User
from shared.models import BaseUUIDModel
from sports.models import Sport, SportPosition


class SportMatchStatField(BaseUUIDModel):
    """
    One loggable stat for one sport — "Goals" for football, "Wickets" for
    cricket. Admin-seeded (``seed_match_stat_fields``), never user-created.

    Retire a stat with ``is_active``, not deletion: ``MatchEntryStat`` points
    here with PROTECT precisely so a delete that would strand logged data
    fails instead of succeeding quietly.
    """

    class ValueType(models.TextChoices):
        INTEGER = "integer", "Integer"
        DECIMAL = "decimal", "Decimal"

    sport = models.ForeignKey(
        Sport,
        on_delete=models.CASCADE,
        related_name="match_stat_fields"
    )

    name = models.CharField(max_length=50)
    # Compact form for the diary row, where twelve stats share a line: "G", "W".
    short_label = models.CharField(max_length=10, blank=True)
    # "km", "%" — display only; the value is always stored as a plain number.
    unit = models.CharField(max_length=20, blank=True)

    value_type = models.CharField(
        max_length=20,
        choices=ValueType.choices
    )

    # Empty means "applies to every position", which is how most stats ship.
    #
    # NOTHING FILTERS ON THIS IN v1. A goalkeeper still sees Goals in the quick-
    # add form, and that is expected, not a bug. The column is here now so that
    # position-aware forms are a serializer change in v1.1 rather than a
    # migration against a table that by then holds every match on the platform.
    positions = models.ManyToManyField(
        SportPosition,
        blank=True,
        related_name="match_stat_fields"
    )

    # The three stats the quick-add form shows by default; the rest sit behind
    # "add more". This flag is the difference between a 30-second log and one
    # nobody finishes.
    is_primary = models.BooleanField(default=False)

    order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "sport_match_stat_fields"
        ordering = ["order", "name"]

        constraints = [
            models.UniqueConstraint(
                fields=["sport", "name"],
                name="unique_sport_match_stat_field"
            ),
        ]

        indexes = [
            models.Index(fields=["sport", "is_active", "order"]),
        ]

    def __str__(self):
        return f"{self.name} ({self.sport.name})"


class MatchEntry(BaseUUIDModel):
    """
    One match — either already played, or scheduled for later.

    The same row serves both, and ``status`` is the only thing separating them.
    Logging a fixture after it is played is a PATCH that flips the status and
    fills in the result; it is never a second create, so the player never ends
    up with the fixture and the report as two rows for one match.

    Soft deleted (``is_deleted``), like every other thing a user can remove:
    a diary is a record, and an accidental swipe should be recoverable.
    """

    class Status(models.TextChoices):
        SCHEDULED = "scheduled", "Scheduled"
        PLAYED = "played", "Played"

    class MatchType(models.TextChoices):
        LEAGUE = "league", "League"
        TOURNAMENT = "tournament", "Tournament"
        FRIENDLY = "friendly", "Friendly"
        SCHOOL_COLLEGE = "school_college", "School / College"
        PRACTICE = "practice", "Practice"
        OTHER = "other", "Other"

    class Result(models.TextChoices):
        WIN = "win", "Win"
        LOSS = "loss", "Loss"
        DRAW = "draw", "Draw"
        NA = "na", "Not applicable"

    class Visibility(models.TextChoices):
        PRIVATE = "private", "Private"
        FOLLOWERS = "followers", "Followers"
        PUBLIC = "public", "Public"

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="match_entries"
    )

    sport = models.ForeignKey(
        Sport,
        on_delete=models.PROTECT,
        related_name="match_entries"
    )

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PLAYED
    )

    date = models.DateField()
    kickoff_time = models.TimeField(null=True, blank=True)

    # Free text. Most opponents at this level are not on Goatza, and a picker
    # that mostly returns nothing is worse than a text box.
    opponent_name = models.CharField(max_length=150, blank=True)

    match_type = models.CharField(
        max_length=20,
        choices=MatchType.choices,
        default=MatchType.OTHER
    )

    result = models.CharField(
        max_length=20,
        choices=Result.choices,
        default=Result.NA
    )

    minutes_played = models.PositiveIntegerField(null=True, blank=True)

    position = models.ForeignKey(
        SportPosition,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="match_entries"
    )

    # 1-5, bounded by match_entry_valid_self_rating below.
    self_rating = models.PositiveSmallIntegerField(null=True, blank=True)

    notes = models.TextField(blank=True)

    # Which club/academy stint this match belongs to. Optional — a player logs
    # matches whether or not they have filled in their career — and SET_NULL,
    # because deleting a career entry must not take the matches with it.
    career_entry = models.ForeignKey(
        "careers.CareerEntry",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="match_entries"
    )

    # 500, not URLField's default 200 — same reason as highlights.Highlight:
    # an actor-scoped Cloudinary public_id path of UUIDs runs ~120 chars and
    # the transform on top lands the URL around 215, which the default rejects
    # at insert time.
    photo_url = models.URLField(max_length=500, blank=True)
    photo_public_id = models.CharField(max_length=255, blank=True)

    # NOT EXPOSED IN v1. The serializer does not accept this field and the list
    # selector filters by owner alone, so every diary is private whatever this
    # column says. It ships now, defaulted to PRIVATE, so that opening the
    # diary up later is a serializer plus selector change — and so that when
    # that day comes, a season already logged in private stays private instead
    # of becoming visible retroactively.
    visibility = models.CharField(
        max_length=20,
        choices=Visibility.choices,
        default=Visibility.PRIVATE
    )

    is_deleted = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "match_entries"
        ordering = ["-date", "-created_at"]

        indexes = [
            models.Index(fields=["user", "is_deleted", "date"]),
            models.Index(fields=["user", "status", "date"]),
            models.Index(fields=["user", "sport", "date"]),
        ]

        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(self_rating__isnull=True) |
                    Q(self_rating__gte=1, self_rating__lte=5)
                ),
                name="match_entry_valid_self_rating"
            ),
            # A scheduled match cannot carry a result, minutes or a rating.
            # The service enforces this too, but it is a real invariant and the
            # DB should hold it: a row claiming a future match was won is worse
            # than a rejected write.
            #
            # Literals rather than Status.PLAYED / Result.NA: Meta is a nested
            # class body and cannot see the enclosing class's names.
            models.CheckConstraint(
                condition=(
                    Q(status="played") |
                    Q(
                        result="na",
                        minutes_played__isnull=True,
                        self_rating__isnull=True
                    )
                ),
                name="match_entry_scheduled_has_no_result"
            ),
        ]

    def __str__(self):
        opponent = self.opponent_name or "unnamed opponent"
        return f"{self.date} vs {opponent} ({self.user_id})"


class MatchEntryStat(BaseUUIDModel):
    """
    One stat value on one match — "Goals: 2".

    Stored as a decimal for every stat, including the integer ones. A single
    column keeps the write path and the season aggregates uniform;
    ``stat_field.value_type`` is what tells the serializer whether to validate
    and render the number as a whole one.
    """

    match_entry = models.ForeignKey(
        MatchEntry,
        on_delete=models.CASCADE,
        related_name="stats"
    )

    # PROTECT, deliberately: deleting a catalog row that players have already
    # logged against should fail loudly rather than silently orphan a season of
    # data. Retiring a stat means SportMatchStatField.is_active = False.
    stat_field = models.ForeignKey(
        SportMatchStatField,
        on_delete=models.PROTECT
    )

    value = models.DecimalField(max_digits=8, decimal_places=2)

    class Meta:
        db_table = "match_entry_stats"

        constraints = [
            models.UniqueConstraint(
                fields=["match_entry", "stat_field"],
                name="unique_stat_per_match_entry"
            ),
        ]

        indexes = [
            models.Index(fields=["match_entry"]),
        ]

    def __str__(self):
        return f"{self.stat_field_id}={self.value} ({self.match_entry_id})"


class MatchDiarySettings(BaseUUIDModel):
    """
    One row per player, created lazily the first time they open the diary, so a
    first-time player reads defaults rather than a 404 — same pattern as
    ``cv.models.PlayerCVSettings``.

    No soft delete. Settings are switched off, never removed.
    """

    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="match_diary_settings"
    )

    # Opt-in surfacing of the season summary on the player's profile. Off by
    # default, and it stays off in v1 — nothing reads it yet.
    showcase_summary = models.BooleanField(default=False)

    # Denormalized, maintained by the service layer on every write, consistent
    # with follower and like counts.
    #
    # Counted in MATCH-WEEKS taken from the match date, not in logging days.
    # Nobody plays daily, so a day-based streak breaks on day two and the
    # number stops meaning anything; a week in which the player logged a match
    # extends the streak, however many matches were in it and whenever they got
    # around to writing them down.
    current_streak_weeks = models.PositiveIntegerField(default=0)
    longest_streak_weeks = models.PositiveIntegerField(default=0)

    # When the player last wrote to the diary — a real timestamp, unlike the
    # streaks above, which are derived from match dates.
    last_logged_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "match_diary_settings"

    def __str__(self):
        return f"Match diary settings - {self.user_id}"
