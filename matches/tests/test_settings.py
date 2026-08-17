"""
The diary settings row: created lazily, read by its owner, and mostly derived.

The lazy create is the part worth protecting. A player who has never opened the
screen has no row, and "your settings do not exist" is not something a settings
screen can render — the model defaults are the answer, so GET makes the row.
"""

from rest_framework import status

from matches.models import MatchDiarySettings
from matches.tests.base import MatchDiaryTestCase


class LazyCreationTests(MatchDiaryTestCase):
    """GET is what brings the row into existence, and only ever one of them."""

    def test_a_first_time_player_has_no_row_until_they_look(self):
        self.assertEqual(MatchDiarySettings.objects.count(), 0)

    def test_get_creates_the_row_and_returns_the_defaults(self):
        resp = self._get_settings()

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(MatchDiarySettings.objects.count(), 1)

        data = resp.data["data"]
        self.assertFalse(data["showcase_summary"])
        self.assertEqual(data["current_streak_weeks"], 0)
        self.assertEqual(data["longest_streak_weeks"], 0)
        self.assertIsNone(data["last_logged_at"])

    def test_a_second_get_does_not_create_a_second_row(self):
        first = self._get_settings()
        second = self._get_settings()

        self.assertEqual(MatchDiarySettings.objects.count(), 1)
        self.assertEqual(first.data["data"], second.data["data"])

    def test_logging_a_match_before_ever_opening_settings_still_works(self):
        """
        The streak writer needs the same row and cannot assume the settings
        screen has been visited first.
        """
        self._played(date=self.monday)

        self.assertEqual(MatchDiarySettings.objects.count(), 1)
        self.assertEqual(
            self._get_settings().data["data"]["current_streak_weeks"], 1
        )


class ShowcaseSummaryToggleTests(MatchDiaryTestCase):
    """The one field a client may write."""

    def test_it_defaults_to_off(self):
        self.assertFalse(
            self._get_settings().data["data"]["showcase_summary"]
        )

    def test_patch_turns_it_on(self):
        resp = self._patch_settings({"showcase_summary": True})

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertTrue(resp.data["data"]["showcase_summary"])
        self.assertTrue(
            MatchDiarySettings.objects.get(user=self.player).showcase_summary
        )

    def test_patch_turns_it_back_off(self):
        self._patch_settings({"showcase_summary": True})

        resp = self._patch_settings({"showcase_summary": False})

        self.assertFalse(resp.data["data"]["showcase_summary"])

    def test_patch_creates_the_row_if_it_is_not_there_yet(self):
        self._patch_settings({"showcase_summary": True})

        self.assertEqual(MatchDiarySettings.objects.count(), 1)

    def test_an_empty_body_is_rejected_rather_than_reporting_success(self):
        """
        A settings screen that says "saved" without saving anything is worse
        than one that errors.
        """
        resp = self._patch_settings({})

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("showcase_summary", resp.data["message"])


class DerivedFieldsAreReadOnlyTests(MatchDiaryTestCase):
    """
    The streak counters are facts about the player's matches. A client that
    could set them would turn them into a claim, and a claim is worth nothing
    on a screen whose whole value is that the number was earned.
    """

    def setUp(self):
        super().setUp()
        self._played(date=self.monday)

    def test_the_streak_counters_cannot_be_written_through_patch(self):
        resp = self._patch_settings({
            "showcase_summary": True,
            "current_streak_weeks": 99,
            "longest_streak_weeks": 99,
        })

        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        settings = MatchDiarySettings.objects.get(user=self.player)
        self.assertEqual(settings.current_streak_weeks, 1)
        self.assertEqual(settings.longest_streak_weeks, 1)

    def test_last_logged_at_cannot_be_written_either(self):
        before = MatchDiarySettings.objects.get(user=self.player).last_logged_at

        self._patch_settings({
            "showcase_summary": True,
            "last_logged_at": "2020-01-01T00:00:00Z",
        })

        after = MatchDiarySettings.objects.get(user=self.player).last_logged_at
        self.assertEqual(before, after)
