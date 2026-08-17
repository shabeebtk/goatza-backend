"""
The season summary — the number the diary exists to produce.

Two things here are easy to get subtly wrong and expensive to notice: what
counts as absent (a null rating is not a zero, an unlogged stat is not a zero
stat) and what ``zero_count`` means. The second is the whole reason this app can
stay sport-agnostic, so it gets its own group.
"""

from datetime import timedelta
from decimal import Decimal

from matches.models import MatchEntry
from matches.services.match_services import MatchService
from matches.tests.base import MatchDiaryTestCase


class SummaryTotalsTests(MatchDiaryTestCase):
    """Counts, results and minutes over a deliberately mixed set."""

    def setUp(self):
        super().setUp()
        self._played(date=self.today, result="win", minutes_played=90)
        self._played(date=self.today - timedelta(days=1), result="win", minutes_played=45)
        self._played(date=self.today - timedelta(days=2), result="loss", minutes_played=90)
        self._played(date=self.today - timedelta(days=3), result="draw", minutes_played=None)
        self._played(date=self.today - timedelta(days=4), result="na", minutes_played=30)

    def test_wins_losses_and_draws_are_counted_separately(self):
        data = self._summary().data["data"]

        self.assertEqual(data["total_matches"], 5)
        self.assertEqual(data["wins"], 2)
        self.assertEqual(data["losses"], 1)
        self.assertEqual(data["draws"], 1)

    def test_a_result_of_na_counts_as_a_match_but_not_as_a_w_l_or_d(self):
        data = self._summary().data["data"]

        self.assertEqual(
            data["total_matches"],
            data["wins"] + data["losses"] + data["draws"] + 1,
        )

    def test_missing_minutes_count_as_zero_rather_than_breaking_the_sum(self):
        data = self._summary().data["data"]

        self.assertEqual(data["minutes_total"], 255)

    def test_a_player_with_no_matches_gets_zeros_not_an_error(self):
        data = self._summary(user=self.other).data["data"]

        self.assertEqual(data["total_matches"], 0)
        self.assertEqual(data["minutes_total"], 0)
        self.assertIsNone(data["average_rating"])
        self.assertEqual(data["form"], [])
        self.assertEqual(data["stats"], [])


class AverageRatingTests(MatchDiaryTestCase):
    """
    A rating is optional, and the average has to say so. Null means "nobody has
    rated a match", which the screen renders as an empty state — treating it as
    zero would show every new player a 0.0 out of 5.
    """

    def test_the_average_ignores_matches_with_no_rating(self):
        self._played(date=self.today, self_rating=4)
        self._played(date=self.today - timedelta(days=1), self_rating=2)
        self._played(date=self.today - timedelta(days=2), self_rating=None)

        data = self._summary().data["data"]

        self.assertEqual(data["average_rating"], 3.0)
        self.assertEqual(data["total_matches"], 3)

    def test_the_average_is_null_when_nothing_has_been_rated(self):
        self._played(self_rating=None)

        self.assertIsNone(self._summary().data["data"]["average_rating"])

    def test_the_average_is_rounded_rather_than_returned_at_full_float_width(self):
        self._played(date=self.today, self_rating=5)
        self._played(date=self.today - timedelta(days=1), self_rating=4)
        self._played(date=self.today - timedelta(days=2), self_rating=4)

        self.assertEqual(self._summary().data["data"]["average_rating"], 4.33)


class FormTests(MatchDiaryTestCase):
    """
    The form chart: the last ten ratings, oldest first, nulls kept.

    Keeping the nulls is the point. A player who rated six of their last ten
    matches should see four gaps — dropping them would draw a ten-match line
    out of six numbers and quietly invent a story.
    """

    def _log(self, ratings):
        """Oldest first, so index 0 is the furthest back."""
        for offset, rating in enumerate(reversed(ratings)):
            self._played(
                date=self.today - timedelta(days=offset),
                self_rating=rating,
            )

    def test_form_reads_oldest_first(self):
        self._log([1, 2, 3])

        self.assertEqual(self._summary().data["data"]["form"], [1, 2, 3])

    def test_form_keeps_the_gaps_where_a_match_was_not_rated(self):
        self._log([5, None, 3, None])

        self.assertEqual(self._summary().data["data"]["form"], [5, None, 3, None])

    def test_form_holds_at_most_ten_and_keeps_the_most_recent_ones(self):
        self._log(list(range(1, 6)) * 3)  # 15 matches

        form = self._summary().data["data"]["form"]

        self.assertEqual(len(form), 10)
        # The last ten of [1..5, 1..5, 1..5], still oldest first.
        self.assertEqual(form, [1, 2, 3, 4, 5, 1, 2, 3, 4, 5])


class ZeroCountTests(MatchDiaryTestCase):
    """
    The clean-sheet path, and the reason this module has no football in it.

    ``zero_count`` counts the matches where a stat was logged AS ZERO, which is
    a different fact from never having logged it. The client reads it off "Goals
    conceded" and prints "2 clean sheets". Nothing in the backend knows what a
    clean sheet is, and nothing should learn.
    """

    def setUp(self):
        super().setUp()
        # A keeper's three matches: two clean sheets, one where they conceded.
        for offset, conceded in enumerate((0, 0, 2)):
            self._played(
                date=self.today - timedelta(days=offset),
                position=self.keeper.id,
                stats=[{"stat_field": self.conceded.id, "value": conceded}],
            )

    def test_zero_count_counts_the_matches_logged_as_zero(self):
        row = self._summary_stats(self._summary())["GC"]

        self.assertEqual(row["zero_count"], 2)

    def test_entries_count_is_every_match_the_stat_was_logged_in(self):
        row = self._summary_stats(self._summary())["GC"]

        self.assertEqual(row["entries_count"], 3)
        self.assertEqual(Decimal(str(row["total"])), Decimal("2.00"))

    def test_a_match_where_the_stat_was_not_logged_is_not_a_zero(self):
        """
        The distinction the whole field rests on. A match with no "Goals
        conceded" row is a match nobody recorded it for, not a clean sheet.
        """
        self._played(date=self.today - timedelta(days=5), position=self.keeper.id)

        row = self._summary_stats(self._summary())["GC"]

        self.assertEqual(row["zero_count"], 2)
        self.assertEqual(row["entries_count"], 3)

    def test_a_stat_never_logged_does_not_appear_at_all(self):
        self.assertNotIn("G", self._summary_stats(self._summary()))

    def test_the_stat_row_carries_what_the_client_needs_to_label_it(self):
        row = self._summary_stats(self._summary())["GC"]

        self.assertEqual(row["name"], "Goals conceded")
        self.assertEqual(row["short_label"], "GC")
        self.assertEqual(row["value_type"], "integer")


class SummaryFilterTests(MatchDiaryTestCase):
    """
    ``year`` and ``sport_id`` partition the summary. Each one narrows on its
    own, and the stats block narrows with it — a season total that quietly
    included another season would be worse than no filter at all.
    """

    def setUp(self):
        super().setUp()
        self._played(
            date=self.today,
            result="win",
            stats=[{"stat_field": self.goals.id, "value": 2}],
        )
        self._played(
            date=self.last_season,
            result="loss",
            stats=[{"stat_field": self.goals.id, "value": 5}],
        )
        self._played(
            date=self.today,
            sport=self.cricket.id,
            result="win",
            stats=[{"stat_field": self.wickets.id, "value": 3}],
        )

    def test_the_unfiltered_summary_covers_everything(self):
        data = self._summary().data["data"]

        self.assertEqual(data["total_matches"], 3)
        self.assertEqual(len(data["stats"]), 2)

    def test_the_year_filter_partitions_by_match_date(self):
        this_year = self._summary(year=self.today.year).data["data"]
        last_year = self._summary(year=self.today.year - 1).data["data"]

        self.assertEqual(this_year["total_matches"], 2)
        self.assertEqual(last_year["total_matches"], 1)
        self.assertEqual(last_year["losses"], 1)

    def test_the_year_filter_narrows_the_stats_block_too(self):
        rows = self._summary_stats(self._summary(year=self.today.year - 1))

        self.assertEqual(Decimal(str(rows["G"]["total"])), Decimal("5.00"))
        self.assertNotIn("W", rows)

    def test_the_sport_filter_partitions_by_sport(self):
        rows = self._summary_stats(self._summary(sport_id=str(self.cricket.id)))

        self.assertEqual(
            self._summary(sport_id=str(self.cricket.id)).data["data"]["total_matches"],
            1,
        )
        self.assertEqual(set(rows), {"W"})

    def test_the_filters_compose(self):
        data = self._summary(
            year=self.today.year,
            sport_id=str(self.football.id),
        ).data["data"]

        self.assertEqual(data["total_matches"], 1)
        self.assertEqual(data["wins"], 1)


class SoftDeletedEntriesAreExcludedTests(MatchDiaryTestCase):
    """
    A deleted match is gone from every number, not just from the list. This is
    the one that catches a filter added to the list selector and forgotten in
    the summary.
    """

    def setUp(self):
        super().setUp()
        self.kept = self._played(
            date=self.today,
            result="win",
            minutes_played=90,
            self_rating=5,
            stats=[{"stat_field": self.goals.id, "value": 2}],
        )
        self.removed = self._played(
            date=self.today - timedelta(days=1),
            result="loss",
            minutes_played=60,
            self_rating=1,
            stats=[{"stat_field": self.goals.id, "value": 7}],
        )
        MatchService.delete_match(self.actor, self.removed.id)

    def test_the_totals_exclude_it(self):
        data = self._summary().data["data"]

        self.assertEqual(data["total_matches"], 1)
        self.assertEqual(data["losses"], 0)
        self.assertEqual(data["minutes_total"], 90)

    def test_the_average_rating_excludes_it(self):
        self.assertEqual(self._summary().data["data"]["average_rating"], 5.0)

    def test_the_form_chart_excludes_it(self):
        self.assertEqual(self._summary().data["data"]["form"], [5])

    def test_the_stats_block_excludes_its_values(self):
        row = self._summary_stats(self._summary())["G"]

        self.assertEqual(Decimal(str(row["total"])), Decimal("2.00"))
        self.assertEqual(row["entries_count"], 1)

    def test_the_stat_rows_themselves_survive_in_the_database(self):
        """
        Soft delete, not cascade: the child rows are still there, and only the
        selector's filter is keeping them out of the numbers.
        """
        self.assertTrue(self.removed.stats.exists())
        self.assertEqual(MatchEntry.objects.count(), 2)
