"""
What may be logged against a match, and what a stat value is allowed to be.

The catalog is the contract. A stat field belongs to exactly one sport and is
either being recorded or retired, and every one of those facts is checked before
a number is stored — a season total assembled from stats that belonged to
another sport is worse than no total at all.

The last group is about the other direction: what happens when somebody tries to
delete a catalog row players have already logged against. PROTECT, loudly.
"""

from decimal import Decimal

from django.db.models import ProtectedError
from rest_framework import status

from matches.models import MatchEntry, MatchEntryStat, SportMatchStatField
from matches.tests.base import MatchDiaryTestCase


class StatFieldMustBelongToTheMatchTests(MatchDiaryTestCase):
    """
    Which catalog rows a match may reference. Anything foreign, retired or
    unknown fails the whole write rather than being dropped from it.
    """

    def test_a_stat_field_from_another_sport_is_rejected(self):
        resp = self._create(body={
            "stats": self._stat_payload((self.wickets, 3)),
        })

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Wickets", resp.data["message"])
        self.assertIn("Football", resp.data["message"])

    def test_a_retired_stat_field_is_rejected(self):
        resp = self._create(body={
            "stats": self._stat_payload((self.retired, 2)),
        })

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("no longer being recorded", resp.data["message"])

    def test_an_unknown_stat_field_id_is_rejected(self):
        resp = self._create(body={
            "stats": [{
                "stat_field_id": "0198f000-0000-7000-8000-000000000000",
                "value": "1",
            }],
        })

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(MatchEntry.objects.count(), 0)

    def test_retiring_a_field_does_not_hide_values_already_logged(self):
        """
        ``is_active`` is about new forms, not about history. A player's past
        matches must keep reading back the same after the admin retires a stat.
        """
        match = self._played(
            stats=[{"stat_field": self.goals.id, "value": 2}]
        )

        self.goals.is_active = False
        self.goals.save(update_fields=["is_active"])

        row = self._results(self._list())[0]
        self.assertEqual(self._stats_by_label(row), {"G": Decimal("2.00")})
        self.assertEqual(
            Decimal(str(self._summary_stats(self._summary())["G"]["total"])),
            Decimal("2.00"),
        )

    def test_a_retired_field_leaves_the_quick_add_catalog(self):
        resp = self._stat_fields(sport_id=str(self.football.id))

        names = [row["name"] for row in self._results(resp)]
        self.assertNotIn("Offsides", names)
        self.assertIn("Goals", names)

    def test_the_catalog_endpoint_requires_a_known_sport(self):
        missing = self._stat_fields()
        unknown = self._stat_fields(
            sport_id="0198f000-0000-7000-8000-000000000000"
        )

        self.assertEqual(missing.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(unknown.status_code, status.HTTP_400_BAD_REQUEST)


class StatValueTypeTests(MatchDiaryTestCase):
    """
    Every stat shares one DecimalField column, so ``value_type`` is the only
    thing standing between "Goals" and a season total in half-goals.
    """

    def test_a_fractional_value_on_an_integer_field_is_rejected(self):
        resp = self._create(body={
            "stats": self._stat_payload((self.goals, "2.5")),
        })

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("whole number", resp.data["message"])

    def test_a_whole_value_on_an_integer_field_is_accepted(self):
        resp = self._create(body={
            "stats": self._stat_payload((self.goals, 3)),
        })

        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

    def test_a_decimal_value_on_a_decimal_field_keeps_its_precision(self):
        resp = self._create(body={
            "stats": self._stat_payload((self.distance, "10.75")),
        })

        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(
            MatchEntryStat.objects.get(stat_field=self.distance).value,
            Decimal("10.75"),
        )

    def test_a_decimal_field_carries_its_unit_through_to_the_summary(self):
        self._played(stats=[{"stat_field": self.distance.id, "value": "9.5"}])

        row = self._summary_stats(self._summary())["DIST"]

        self.assertEqual(row["unit"], "km")
        self.assertEqual(row["value_type"], SportMatchStatField.ValueType.DECIMAL)

    def test_a_negative_value_is_rejected(self):
        resp = self._create(body={
            "stats": self._stat_payload((self.goals, -1)),
        })

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_value_too_large_for_the_column_is_a_400_not_a_500(self):
        resp = self._create(body={
            "stats": [{"stat_field_id": str(self.goals.id), "value": "99999999"}],
        })

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)


class DuplicateStatFieldsTests(MatchDiaryTestCase):
    """
    Two values for one stat in one payload is a client bug, not a preference.
    Only one of them could survive the unique constraint anyway, and quietly
    keeping the second is how a player ends up disputing their own total.
    """

    def test_the_same_stat_twice_in_one_payload_is_rejected(self):
        resp = self._create(body={
            "stats": self._stat_payload((self.goals, 1), (self.goals, 2)),
        })

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("twice", resp.data["message"])

    def test_nothing_is_written_when_a_duplicate_is_refused(self):
        self._create(body={
            "stats": self._stat_payload((self.goals, 1), (self.goals, 2)),
        })

        self.assertEqual(MatchEntry.objects.count(), 0)
        self.assertEqual(MatchEntryStat.objects.count(), 0)

    def test_a_duplicate_is_refused_on_update_too(self):
        match = self._played()

        resp = self._update(match.id, {
            "stats": self._stat_payload((self.assists, 1), (self.assists, 3)),
        })

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(match.stats.count(), 0)


class CatalogRowsInUseAreProtectedTests(MatchDiaryTestCase):
    """
    Deleting a stat players have logged against must fail loudly rather than
    silently orphaning a season of data. Retiring via ``is_active`` is the
    supported way out.
    """

    def setUp(self):
        super().setUp()
        self.match = self._played(
            stats=[{"stat_field": self.goals.id, "value": 2}]
        )

    def test_deleting_a_stat_field_in_use_raises_protectederror(self):
        with self.assertRaises(ProtectedError):
            self.goals.delete()

    def test_an_unused_stat_field_can_still_be_deleted(self):
        self.assists.delete()

        self.assertFalse(
            SportMatchStatField.objects.filter(id=self.assists.id).exists()
        )

    def test_deleting_the_match_leaves_the_catalog_row_alone(self):
        """
        CASCADE runs the other way: a match takes its own stat rows with it,
        and the catalog is untouched.
        """
        self.match.stats.all().delete()
        self.goals.refresh_from_db()

        self.assertTrue(
            SportMatchStatField.objects.filter(id=self.goals.id).exists()
        )
