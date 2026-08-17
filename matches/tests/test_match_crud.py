"""
The everyday paths: log a match, schedule one, promote it, edit it, delete it.

The promotion test is the one that matters most. A fixture and the report of the
match it became are ONE row, and the whole design falls apart if a player ends
up with two — so "one PATCH carrying status, result, minutes, rating and stats"
is tested as the single call it is meant to be, not as a sequence.
"""

from datetime import timedelta
from decimal import Decimal

from django.test.utils import CaptureQueriesContext
from django.db import connection
from rest_framework import status

from matches.models import MatchEntry
from matches.tests.base import MatchDiaryTestCase


class CreateMatchTests(MatchDiaryTestCase):
    """
    What a create accepts. Only sport and date are required — the quick-add
    form's whole premise is that a match can be logged in 30 seconds.
    """

    def test_a_played_match_stores_its_nested_stats(self):
        resp = self._create(body={
            "result": "win",
            "minutes_played": 90,
            "self_rating": 5,
            "position": str(self.striker.id),
            "stats": self._stat_payload(
                (self.goals, 2),
                (self.assists, 1),
                (self.conceded, 0),
            ),
        })

        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

        row = resp.data["data"]
        self.assertEqual(row["status"], MatchEntry.Status.PLAYED)
        self.assertEqual(row["position"]["name"], "Striker")
        self.assertEqual(
            self._stats_by_label(row),
            {"G": Decimal("2.00"), "A": Decimal("1.00"), "GC": Decimal("0.00")},
        )

    def test_stats_read_back_in_catalog_order_not_payload_order(self):
        """
        The compact diary row prints "G 2 · A 1", and which order that is must
        not depend on how the client happened to build its body.
        """
        resp = self._create(body={
            "stats": self._stat_payload(
                (self.conceded, 0),
                (self.goals, 2),
                (self.assists, 1),
            ),
        })

        labels = [row["short_label"] for row in resp.data["data"]["stats"]]
        self.assertEqual(labels, ["G", "A", "GC"])

    def test_a_fixture_needs_only_a_date_an_opponent_and_a_type(self):
        resp = self._create(body={
            "status": "scheduled",
            "date": str(self.today + timedelta(days=5)),
            "opponent_name": "Riverside FC",
            "match_type": "league",
        })

        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

        row = resp.data["data"]
        self.assertEqual(row["status"], MatchEntry.Status.SCHEDULED)
        self.assertEqual(row["result"], MatchEntry.Result.NA)
        self.assertIsNone(row["minutes_played"])
        self.assertIsNone(row["self_rating"])
        self.assertEqual(row["stats"], [])

    def test_played_is_the_default_status(self):
        resp = self._create()

        self.assertEqual(resp.data["data"]["status"], MatchEntry.Status.PLAYED)

    def test_a_position_from_another_sport_is_rejected(self):
        resp = self._create(body={
            "sport": str(self.cricket.id),
            "position": str(self.keeper.id),
        })

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Goalkeeper", resp.data["message"])
        self.assertEqual(MatchEntry.objects.count(), 0)

    def test_a_rejected_stat_leaves_no_half_logged_match(self):
        resp = self._create(body={
            "stats": self._stat_payload((self.wickets, 3)),
        })

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(MatchEntry.objects.count(), 0)


class PromoteFixtureTests(MatchDiaryTestCase):
    """
    THE primary user path. A fixture becomes a played match in one PATCH, and
    stays the same row while doing it.
    """

    def setUp(self):
        super().setUp()
        self.fixture = self._fixture(
            date=self.today - timedelta(days=1),
            position=self.striker.id,
        )

    def test_one_patch_carries_status_result_minutes_rating_and_stats(self):
        resp = self._update(self.fixture.id, {
            "status": "played",
            "result": "win",
            "minutes_played": 85,
            "self_rating": 4,
            "notes": "Ran the channel all game.",
            "stats": self._stat_payload((self.goals, 1), (self.assists, 2)),
        })

        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        row = resp.data["data"]
        self.assertEqual(row["status"], MatchEntry.Status.PLAYED)
        self.assertEqual(row["result"], MatchEntry.Result.WIN)
        self.assertEqual(row["minutes_played"], 85)
        self.assertEqual(row["self_rating"], 4)
        self.assertEqual(
            self._stats_by_label(row),
            {"G": Decimal("1.00"), "A": Decimal("2.00")},
        )

    def test_promoting_does_not_create_a_second_row(self):
        self._update(self.fixture.id, {
            "status": "played",
            "result": "draw",
            "stats": self._stat_payload((self.goals, 0)),
        })

        self.assertEqual(MatchEntry.objects.filter(user=self.player).count(), 1)
        self.assertEqual(str(MatchEntry.objects.get().id), str(self.fixture.id))

    def test_promoting_keeps_what_the_fixture_already_carried(self):
        self._update(self.fixture.id, {"status": "played", "result": "win"})

        self.fixture.refresh_from_db()
        self.assertEqual(self.fixture.opponent_name, "Riverside FC")
        self.assertEqual(self.fixture.position_id, self.striker.id)

    def test_a_promoted_fixture_leaves_the_upcoming_strip(self):
        self.assertEqual(self._upcoming().data["data"]["count"], 1)

        self._update(self.fixture.id, {"status": "played", "result": "win"})

        self.assertEqual(self._upcoming().data["data"]["count"], 0)

    def test_an_unplayed_fixture_in_the_past_is_flagged_overdue(self):
        """
        The prompt to go and log it. upcoming_matches deliberately does not
        filter past fixtures out, so this flag is what the client renders on.
        """
        resp = self._upcoming()

        self.assertTrue(self._results(resp)[0]["is_overdue"])

    def test_a_future_fixture_is_not_overdue(self):
        self._fixture(date=self.today + timedelta(days=4), opponent_name="Later FC")

        rows = {r["opponent_name"]: r["is_overdue"] for r in self._results(self._upcoming())}

        self.assertFalse(rows["Later FC"])


class UpdateMatchTests(MatchDiaryTestCase):
    """
    PATCH semantics, and the one rule a client is most likely to get wrong:
    ``stats`` is replace-on-update, and an ABSENT key is not an empty one.
    """

    def setUp(self):
        super().setUp()
        self.match = self._played(
            stats=[
                {"stat_field": self.goals.id, "value": 2},
                {"stat_field": self.assists.id, "value": 1},
            ]
        )

    def test_sending_stats_replaces_the_whole_set(self):
        resp = self._update(self.match.id, {
            "stats": self._stat_payload((self.goals, 3)),
        })

        self.assertEqual(
            self._stats_by_label(resp.data["data"]),
            {"G": Decimal("3.00")},
        )
        self.assertEqual(self.match.stats.count(), 1)

    def test_sending_an_empty_stats_list_clears_them(self):
        resp = self._update(self.match.id, {"stats": []})

        self.assertEqual(resp.data["data"]["stats"], [])
        self.assertEqual(self.match.stats.count(), 0)

    def test_omitting_stats_leaves_them_untouched(self):
        """
        The typo-fix case. A PATCH that only means to change the notes must not
        wipe a match's stats on its way through.
        """
        resp = self._update(self.match.id, {"notes": "fixed a typo"})

        self.assertEqual(
            self._stats_by_label(resp.data["data"]),
            {"G": Decimal("2.00"), "A": Decimal("1.00")},
        )

    def test_an_empty_body_is_rejected(self):
        resp = self._update(self.match.id, {})

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_position_from_another_sport_is_rejected_on_update(self):
        resp = self._update(self.match.id, {"position": str(self.batsman.id)})

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.match.refresh_from_db()
        self.assertIsNone(self.match.position_id)

    def test_changing_sport_while_stats_are_logged_is_rejected(self):
        resp = self._update(self.match.id, {"sport": str(self.cricket.id)})

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("stats", resp.data["message"])

    def test_changing_sport_is_allowed_when_stats_come_with_it(self):
        resp = self._update(self.match.id, {
            "sport": str(self.cricket.id),
            "stats": self._stat_payload((self.wickets, 3)),
        })

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["data"]["sport"]["name"], "Cricket")
        self.assertEqual(self._stats_by_label(resp.data["data"]), {"W": Decimal("3.00")})


class DeleteMatchTests(MatchDiaryTestCase):
    """
    Soft delete, consistent with posts, messages and highlights. The row
    survives; every surface behaves as though it did not.
    """

    def setUp(self):
        super().setUp()
        self.played = self._played(minutes_played=90, self_rating=4)
        self.fixture = self._fixture()

    def test_delete_soft_deletes_rather_than_removing_the_row(self):
        resp = self._delete(self.played.id)

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.played.refresh_from_db()
        self.assertTrue(self.played.is_deleted)

    def test_a_deleted_match_disappears_from_the_list(self):
        self._delete(self.played.id)

        self.assertNotIn(str(self.played.id), self._ids(self._list()))

    def test_a_deleted_fixture_disappears_from_upcoming(self):
        self._delete(self.fixture.id)

        self.assertEqual(self._upcoming().data["data"]["count"], 0)

    def test_a_deleted_match_disappears_from_the_summary(self):
        before = self._summary().data["data"]
        self.assertEqual(before["total_matches"], 1)
        self.assertEqual(before["minutes_total"], 90)

        self._delete(self.played.id)

        after = self._summary().data["data"]
        self.assertEqual(after["total_matches"], 0)
        self.assertEqual(after["minutes_total"], 0)
        self.assertIsNone(after["average_rating"])

    def test_deleting_twice_reads_as_gone(self):
        self._delete(self.played.id)

        resp = self._delete(self.played.id)

        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)


class MatchListFilterTests(MatchDiaryTestCase):
    """
    The diary list's three filters, and its page shape. Each one narrows
    independently — a season switcher must not also change which sport is shown.
    """

    def setUp(self):
        super().setUp()
        self.this_year = self._played(date=self.today, opponent_name="Now FC")
        self.last_year = self._played(
            date=self.last_season,
            opponent_name="Then FC",
        )
        self.cricket_match = self._played(
            sport=self.cricket.id, opponent_name="Willow CC"
        )
        self.fixture = self._fixture(opponent_name="Next FC")

    def _names(self, resp):
        return sorted(row["opponent_name"] for row in self._results(resp))

    def test_the_page_carries_count_limit_offset_and_results(self):
        resp = self._list()

        self.assertEqual(
            set(resp.data["data"].keys()),
            {"count", "limit", "offset", "results"},
        )
        self.assertEqual(resp.data["data"]["count"], 4)
        self.assertEqual(resp.data["data"]["limit"], 20)

    def test_newest_match_first(self):
        order = [row["opponent_name"] for row in self._results(self._list())]

        # The fixture is three days out, so it leads; last year's match trails.
        self.assertEqual(order[0], "Next FC")
        self.assertEqual(order[-1], "Then FC")

    def test_status_filter_splits_played_from_scheduled(self):
        self.assertEqual(
            self._names(self._list(status="scheduled")), ["Next FC"]
        )
        self.assertEqual(
            self._names(self._list(status="played")),
            ["Now FC", "Then FC", "Willow CC"],
        )

    def test_year_filter_uses_the_match_date(self):
        resp = self._list(year=self.today.year - 1)

        self.assertEqual(self._names(resp), ["Then FC"])

    def test_sport_filter_narrows_to_one_sport(self):
        resp = self._list(sport_id=str(self.cricket.id))

        self.assertEqual(self._names(resp), ["Willow CC"])

    def test_filters_compose_rather_than_override_each_other(self):
        resp = self._list(
            status="played",
            year=self.today.year,
            sport_id=str(self.football.id),
        )

        self.assertEqual(self._names(resp), ["Now FC"])

    def test_limit_and_offset_page_through(self):
        first = self._list(limit=2, offset=0)
        second = self._list(limit=2, offset=2)

        self.assertEqual(first.data["data"]["count"], 4)
        self.assertEqual(len(self._results(first)), 2)
        self.assertEqual(len(self._results(second)), 2)
        self.assertFalse(set(self._ids(first)) & set(self._ids(second)))

    def test_an_over_large_limit_is_clamped_not_rejected(self):
        resp = self._list(limit=500)

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["data"]["limit"], 50)

    def test_junk_query_parameters_are_400s_with_a_readable_message(self):
        for params, expected in (
            ({"year": "banana"}, "year"),
            ({"limit": "lots"}, "limit"),
            ({"offset": "later"}, "offset"),
            ({"sport_id": "not-a-uuid"}, "sport"),
            ({"status": "maybe"}, "status"),
        ):
            with self.subTest(params=params):
                resp = self._list(**params)
                self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertIn(expected, resp.data["message"].lower())

    def test_an_unknown_sport_filter_is_an_empty_page_not_an_error(self):
        resp = self._list(sport_id="0198f000-0000-7000-8000-000000000000")

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["data"]["count"], 0)


class VisibilityIsNotSettableTests(MatchDiaryTestCase):
    """
    The invariant that protects the v1.1 rollout from being retroactive.

    ``MatchEntry.visibility`` exists in the schema so that opening the diary up
    later is a serializer change rather than a migration. If a client could set
    it now, that day would silently publish matches logged by players who
    believed the diary was private. It must not be reachable from any endpoint.
    """

    def test_visibility_in_a_create_body_does_not_reach_the_row(self):
        resp = self._create(body={"visibility": "public"})

        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(
            MatchEntry.objects.get().visibility,
            MatchEntry.Visibility.PRIVATE,
        )

    def test_visibility_in_an_update_body_does_not_reach_the_row(self):
        match = self._played()

        resp = self._update(match.id, {
            "visibility": "followers",
            "notes": "and make me visible",
        })

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        match.refresh_from_db()
        self.assertEqual(match.visibility, MatchEntry.Visibility.PRIVATE)
        self.assertEqual(match.notes, "and make me visible")

    def test_visibility_is_never_returned_either(self):
        resp = self._create()

        self.assertNotIn("visibility", resp.data["data"])


class ListQueryCountTests(MatchDiaryTestCase):
    """
    A diary page must cost the same whether it holds one match or fifty. This
    pins the prefetching: without it, twenty rows carrying three stats each is
    sixty extra queries and the page dies the week a player starts using it.
    """

    def test_a_page_of_twenty_matches_with_three_stats_each_is_bounded(self):
        for index in range(20):
            self._played(
                date=self.today - timedelta(days=index),
                opponent_name=f"Team {index}",
                stats=[
                    {"stat_field": self.goals.id, "value": index % 3},
                    {"stat_field": self.assists.id, "value": 1},
                    {"stat_field": self.conceded.id, "value": 0},
                ],
            )

        self._auth(self.player)

        # count + the page (sport/position/career_entry joined in) + the stats
        # prefetch, which carries its own stat_field join.
        with CaptureQueriesContext(connection) as ctx:
            resp = self.client.get("/matches/list?limit=20")

        self.assertEqual(resp.data["data"]["count"], 20)
        self.assertEqual(len(self._results(resp)), 20)
        self.assertEqual(len(self._results(resp)[0]["stats"]), 3)
        self.assertEqual(len(ctx.captured_queries), 3)
