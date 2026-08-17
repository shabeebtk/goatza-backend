"""
The one invariant the database also holds: a fixture has not happened yet.

A row saying a future match was won is worse than a rejected write — it is a
claim nobody made, sitting in the record a recruiter reads. So it is enforced
twice: once by the service, with a sentence a player can act on, and once by
``match_entry_scheduled_has_no_result``, which catches anything that reaches the
table by another route.

The constraint test is the important half. Service checks are only as good as
every future caller remembering to go through the service.
"""

from datetime import timedelta

from django.db import IntegrityError, transaction
from rest_framework import status

from matches.models import MatchEntry
from matches.tests.base import MatchDiaryTestCase


class ScheduledMatchesCarryNothingTests(MatchDiaryTestCase):
    """
    Each of the four things a fixture cannot have, refused individually so a
    regression names the field that broke rather than "something".
    """

    def _scheduled(self, **extra):
        body = {
            "status": "scheduled",
            "date": str(self.today + timedelta(days=3)),
        }
        body.update(extra)
        return self._create(body=body)

    def test_a_result_on_a_fixture_is_rejected(self):
        resp = self._scheduled(result="win")

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("result", resp.data["message"])

    def test_minutes_played_on_a_fixture_is_rejected(self):
        resp = self._scheduled(minutes_played=90)

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("minutes", resp.data["message"])

    def test_a_self_rating_on_a_fixture_is_rejected(self):
        resp = self._scheduled(self_rating=5)

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("rating", resp.data["message"])

    def test_stats_on_a_fixture_are_rejected(self):
        resp = self._scheduled(stats=self._stat_payload((self.goals, 1)))

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("stats", resp.data["message"])

    def test_the_message_lists_everything_that_was_wrong_at_once(self):
        resp = self._scheduled(result="win", minutes_played=90, self_rating=3)

        message = resp.data["message"]
        self.assertIn("result", message)
        self.assertIn("minutes", message)
        self.assertIn("rating", message)

    def test_nothing_is_written_when_a_fixture_is_refused(self):
        self._scheduled(result="win")

        self.assertEqual(MatchEntry.objects.count(), 0)

    def test_a_fixture_that_stays_scheduled_cannot_gain_a_result_by_patch(self):
        fixture = self._fixture()

        resp = self._update(fixture.id, {"result": "win"})

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        fixture.refresh_from_db()
        self.assertEqual(fixture.result, MatchEntry.Result.NA)


class ScheduledConstraintHoldsWithoutTheServiceTests(MatchDiaryTestCase):
    """
    The database backstop. These bypass MatchService entirely — a management
    command, a data migration or a shell session must not be able to write a
    fixture that claims a result.
    """

    def _direct(self, **fields):
        return MatchEntry.objects.create(
            user=self.player,
            sport=self.football,
            status=MatchEntry.Status.SCHEDULED,
            date=self.today + timedelta(days=2),
            **fields,
        )

    def test_a_direct_create_with_a_result_raises_integrityerror(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self._direct(result=MatchEntry.Result.WIN)

    def test_a_direct_create_with_minutes_raises_integrityerror(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self._direct(minutes_played=90)

    def test_a_direct_create_with_a_rating_raises_integrityerror(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self._direct(self_rating=4)

    def test_a_clean_fixture_is_accepted(self):
        with transaction.atomic():
            entry = self._direct()

        self.assertEqual(entry.status, MatchEntry.Status.SCHEDULED)

    def test_the_rating_range_is_also_a_database_constraint(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                MatchEntry.objects.create(
                    user=self.player,
                    sport=self.football,
                    status=MatchEntry.Status.PLAYED,
                    date=self.today,
                    self_rating=9,
                )


class PlayedCannotGoBackToScheduledTests(MatchDiaryTestCase):
    """
    Demotion is refused rather than applied.

    Applying it would mean throwing away the result, minutes, rating and stats
    the player already logged — silently deleting somebody's own record of a
    match is worse than an error that tells them what to do instead.
    """

    def setUp(self):
        super().setUp()
        self.match = self._played(
            self_rating=5,
            stats=[{"stat_field": self.goals.id, "value": 2}],
        )

    def test_moving_played_back_to_scheduled_is_rejected(self):
        resp = self._update(self.match.id, {"status": "scheduled"})

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_the_message_says_what_to_do_instead(self):
        resp = self._update(self.match.id, {"status": "scheduled"})

        message = resp.data["message"].lower()
        self.assertIn("delete", message)
        self.assertIn("date", message)

    def test_the_logged_data_survives_the_refusal(self):
        self._update(self.match.id, {"status": "scheduled"})

        self.match.refresh_from_db()
        self.assertEqual(self.match.status, MatchEntry.Status.PLAYED)
        self.assertEqual(self.match.result, MatchEntry.Result.WIN)
        self.assertEqual(self.match.self_rating, 5)
        self.assertEqual(self.match.stats.count(), 1)


class FixturesAreNotCountedTests(MatchDiaryTestCase):
    """
    A fixture is a plan, not a match. It must not reach the summary or the
    streak — a player who scheduled six games has not played six games.
    """

    def setUp(self):
        super().setUp()
        self._played(minutes_played=90, result="win")
        self._fixture(date=self.today + timedelta(days=2))
        self._fixture(date=self.today + timedelta(days=9))

    def test_the_summary_counts_only_played_matches(self):
        data = self._summary().data["data"]

        self.assertEqual(data["total_matches"], 1)
        self.assertEqual(data["wins"], 1)
        self.assertEqual(data["minutes_total"], 90)

    def test_fixtures_do_not_extend_the_streak(self):
        """
        Two fixtures land in the two weeks after this one. If they counted,
        the longest streak would read 3.
        """
        data = self._get_settings().data["data"]

        self.assertEqual(data["longest_streak_weeks"], 1)

    def test_fixtures_still_appear_in_the_list_and_in_upcoming(self):
        self.assertEqual(self._list().data["data"]["count"], 3)
        self.assertEqual(self._upcoming().data["data"]["count"], 2)
