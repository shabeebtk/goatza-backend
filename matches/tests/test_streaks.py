"""
Streaks, which are counted in MATCH-WEEKS from the match date.

Every test here anchors on ``self.monday`` — the Monday of the current ISO week
— so none of them depend on which day of the week the suite happens to run.

The two rules worth stating out loud, because both are easy to "fix" into
something worse:

  * The grace rule. Not having played yet THIS week does not break a streak that
    ran through last week. On a Tuesday, a player who has not played has not
    failed at anything.
  * Match date, not logging date. A player who sits down on Sunday and logs four
    weeks of backlog genuinely played four weeks running. A day-based streak
    would break on day two and would reward opening the app instead of playing.
"""

from datetime import timedelta

from django.db import connection
from django.test.utils import CaptureQueriesContext

from matches.models import MatchDiarySettings
from matches.services.match_services import MatchService
from matches.tests.base import MatchDiaryTestCase


class ConsecutiveWeeksTests(MatchDiaryTestCase):
    """The basic count: one match in a week extends the run by one."""

    def _settings(self):
        return MatchDiarySettings.objects.get(user=self.player)

    def test_three_consecutive_match_weeks_give_a_streak_of_three(self):
        for weeks_back in (0, 1, 2):
            self._played(date=self.monday - timedelta(weeks=weeks_back))

        settings = self._settings()
        self.assertEqual(settings.current_streak_weeks, 3)
        self.assertEqual(settings.longest_streak_weeks, 3)

    def test_several_matches_in_one_week_still_count_as_one_week(self):
        self._played(date=self.monday)
        self._played(date=self.monday + timedelta(days=1))
        self._played(date=self.monday + timedelta(days=2))

        self.assertEqual(self._settings().current_streak_weeks, 1)

    def test_a_gap_resets_the_current_streak_but_not_the_longest(self):
        # A four-week run, then a missed week, then this week.
        for weeks_back in (0, 2, 3, 4, 5):
            self._played(date=self.monday - timedelta(weeks=weeks_back))

        settings = self._settings()
        self.assertEqual(settings.current_streak_weeks, 1)
        self.assertEqual(settings.longest_streak_weeks, 4)

    def test_the_longest_streak_is_the_best_run_anywhere_in_the_history(self):
        for weeks_back in (30, 31, 32, 33, 34):
            self._played(date=self.monday - timedelta(weeks=weeks_back))

        settings = self._settings()
        self.assertEqual(settings.longest_streak_weeks, 5)
        self.assertEqual(settings.current_streak_weeks, 0)

    def test_a_player_with_no_entries_reads_zero_rather_than_crashing(self):
        settings = self._get_settings().data["data"]

        self.assertEqual(settings["current_streak_weeks"], 0)
        self.assertEqual(settings["longest_streak_weeks"], 0)

    def test_a_streak_survives_the_turn_of_the_year(self):
        """
        ISO weeks, not calendar ones. A run through the New Year must not
        restart just because the year number changed.

        2024 has 52 ISO weeks, so (2024, 52) is immediately followed by
        (2025, 1) — the pair a naive "same year, week + 1" would split.
        """
        weeks = {(2024, 51), (2024, 52), (2025, 1)}

        self.assertEqual(MatchService._longest_streak(weeks), 3)

    def test_weeks_either_side_of_a_year_gap_are_not_joined(self):
        weeks = {(2024, 52), (2025, 3)}

        self.assertEqual(MatchService._longest_streak(weeks), 1)


class GraceWeekTests(MatchDiaryTestCase):
    """
    The grace rule, tested on its own because it is the one a well-meaning
    refactor deletes.
    """

    def test_not_having_played_yet_this_week_does_not_break_the_streak(self):
        # Nothing this week; three matches in the three weeks before it.
        for weeks_back in (1, 2, 3):
            self._played(date=self.monday - timedelta(weeks=weeks_back))

        settings = MatchDiarySettings.objects.get(user=self.player)

        self.assertEqual(settings.current_streak_weeks, 3)

    def test_the_grace_is_exactly_one_week_wide(self):
        """
        Last week counts as "not broken yet". The week before that does not —
        two silent weeks is a streak that ended.
        """
        for weeks_back in (2, 3, 4):
            self._played(date=self.monday - timedelta(weeks=weeks_back))

        settings = MatchDiarySettings.objects.get(user=self.player)

        self.assertEqual(settings.current_streak_weeks, 0)
        self.assertEqual(settings.longest_streak_weeks, 3)

    def test_playing_this_week_after_the_grace_week_extends_rather_than_restarts(self):
        for weeks_back in (1, 2):
            self._played(date=self.monday - timedelta(weeks=weeks_back))

        self.assertEqual(
            MatchDiarySettings.objects.get(user=self.player).current_streak_weeks,
            2,
        )

        self._played(date=self.monday)

        self.assertEqual(
            MatchDiarySettings.objects.get(user=self.player).current_streak_weeks,
            3,
        )


class StreaksFollowMatchDateNotLoggingDateTests(MatchDiaryTestCase):
    """
    The rule that makes the counter mean "played", not "opened the app".

    Every match in this group is written in one sitting — the rows are created
    seconds apart — and the streak still reads four, because it is built from
    the dates the matches were played on.
    """

    def test_four_weeks_of_backlog_logged_at_once_give_a_streak_of_four(self):
        for weeks_back in (0, 1, 2, 3):
            self._played(date=self.monday - timedelta(weeks=weeks_back))

        settings = MatchDiarySettings.objects.get(user=self.player)

        self.assertEqual(settings.current_streak_weeks, 4)
        self.assertEqual(settings.longest_streak_weeks, 4)

    def test_logging_the_same_week_repeatedly_does_not_farm_a_streak(self):
        for _ in range(6):
            self._played(date=self.monday)

        self.assertEqual(
            MatchDiarySettings.objects.get(user=self.player).current_streak_weeks,
            1,
        )

    def test_last_logged_at_is_stamped_even_though_the_streak_is_not_about_it(self):
        self._played(date=self.monday)

        settings = MatchDiarySettings.objects.get(user=self.player)

        self.assertIsNotNone(settings.last_logged_at)


class StreakRecomputationTests(MatchDiaryTestCase):
    """
    The streak is derived, not accumulated. Anything that changes which weeks
    have a played match has to move it — including edits and deletes, which is
    where an incremental counter would quietly drift.
    """

    def setUp(self):
        super().setUp()
        self.week0 = self._played(date=self.monday)
        self.week1 = self._played(date=self.monday - timedelta(weeks=1))
        self.week2 = self._played(date=self.monday - timedelta(weeks=2))

    def _settings(self):
        return MatchDiarySettings.objects.get(user=self.player)

    def test_the_starting_state_is_a_three_week_streak(self):
        self.assertEqual(self._settings().current_streak_weeks, 3)

    def test_deleting_the_middle_week_recomputes_the_streak(self):
        self._delete(self.week1.id)

        settings = self._settings()
        self.assertEqual(settings.current_streak_weeks, 1)
        self.assertEqual(settings.longest_streak_weeks, 1)

    def test_deleting_the_oldest_week_shortens_the_run(self):
        self._delete(self.week2.id)

        self.assertEqual(self._settings().current_streak_weeks, 2)

    def test_moving_an_entrys_date_out_of_the_run_recomputes_it(self):
        self._update(
            self.week1.id,
            {"date": str(self.monday - timedelta(weeks=20))},
        )

        settings = self._settings()
        self.assertEqual(settings.current_streak_weeks, 1)
        self.assertEqual(settings.longest_streak_weeks, 1)

    def test_moving_a_date_to_close_a_gap_extends_the_run(self):
        self._delete(self.week1.id)
        self.assertEqual(self._settings().current_streak_weeks, 1)

        # Drag week 2's match forward into the empty week 1.
        self._update(
            self.week2.id,
            {"date": str(self.monday - timedelta(weeks=1))},
        )

        self.assertEqual(self._settings().current_streak_weeks, 2)

    def test_promoting_a_fixture_moves_the_streak(self):
        """
        A fixture is not a match. The streak only moves when it becomes one.
        """
        fixture = self._fixture(date=self.monday - timedelta(weeks=3))
        self.assertEqual(self._settings().current_streak_weeks, 3)

        self._update(fixture.id, {"status": "played", "result": "win"})

        self.assertEqual(self._settings().current_streak_weeks, 4)

    def test_the_longest_streak_can_come_back_down(self):
        """
        Recomputed from scratch, never accumulated: deleting matches has to be
        able to reduce it, or it stops being a fact about the player.
        """
        self.assertEqual(self._settings().longest_streak_weeks, 3)

        self._delete(self.week1.id)

        self.assertEqual(self._settings().longest_streak_weeks, 1)


class StreakQueryShapeTests(MatchDiaryTestCase):
    """
    The weeks are grouped in the database, not walked row by row in Python — a
    player with three seasons logged must not make every write slower.
    """

    def test_recomputing_costs_a_fixed_number_of_queries(self):
        for weeks_back in range(30):
            self._played(date=self.monday - timedelta(weeks=weeks_back))

        with CaptureQueriesContext(connection) as ctx:
            MatchService.recompute_streak(self.player)

        # the distinct weeks, the settings row, the settings update
        self.assertEqual(len(ctx.captured_queries), 3)
