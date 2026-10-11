"""
CELERY_BEAT_SCHEDULE — that every entry points at a task that exists.

WHY THIS IS WORTH A TEST. A beat entry names its task by STRING. Celery
publishes that name whether or not anything is registered under it, and the
publisher never finds out: beat logs a successful send, the worker answers
``NotRegistered`` on the other side of the broker, and the job is dropped. A
typo, a renamed task or a ``tasks.py`` that stopped being imported all look
exactly like a working schedule until somebody notices the purge has not run
for a month.

So this is a wiring test, not a behaviour test — every assertion is about
names, times and the expiry window, and nothing here runs a job.
"""

from django.conf import settings
from django.test import TestCase

from core.celery import app as celery_app

# The entries that spend money or destroy data, and the minute each must keep.
# send_trial_reminders is the sharp one: the command is GATED on the hour, so
# an entry at :30 would run on time, find the window shut, and report nothing
# wrong — every hour, forever.
EXPECTED_MINUTES = {
    "recruitments.send_trial_reminders": 0,
    "places.refresh_place_coords": 30,
    "accounts.purge_deleted_accounts": 0,
    "guardians.purge_unconsented": 15,
    "accounts.downgrade_precise_locations": 30,
}


class BeatScheduleTests(TestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # What a worker does on boot. Without it the registry holds only the
        # tasks this process happened to import, and the test would pass by
        # accident for anything already pulled in by another module.
        celery_app.loader.import_default_modules()

    def test_every_scheduled_task_is_registered(self):
        missing = [
            entry["task"]
            for entry in settings.CELERY_BEAT_SCHEDULE.values()
            if entry["task"] not in celery_app.tasks
        ]

        self.assertEqual(missing, [], f"scheduled but not registered: {missing}")

    def test_the_entry_key_matches_the_task_name(self):
        # Not required by Celery, but a key that drifts from its task is how a
        # schedule ends up quietly running the wrong job.
        mismatched = {
            key: entry["task"]
            for key, entry in settings.CELERY_BEAT_SCHEDULE.items()
            if key != entry["task"]
        }

        self.assertEqual(mismatched, {})

    def test_every_entry_expires(self):
        """
        Without ``expires`` a worker that was down for an hour comes back to
        sixty queued heartbeats and twelve announcement drains and runs the
        lot. With it the broker drops the ticks that are already pointless.
        """
        without = [
            key for key, entry in settings.CELERY_BEAT_SCHEDULE.items()
            if not entry.get("options", {}).get("expires")
        ]

        self.assertEqual(without, [])

    def test_the_heartbeat_expires_faster_than_the_stale_threshold(self):
        # A heartbeat is a statement about NOW. A stale tick executed late
        # would write a fresh timestamp for a moment that has passed, which
        # reports a dead worker as healthy.
        heartbeat = settings.CELERY_BEAT_SCHEDULE["core.heartbeat"]

        self.assertLess(
            heartbeat["options"]["expires"], settings.CELERY_WORKER_STALE_AFTER
        )

    def test_the_heartbeat_ticks_well_inside_the_stale_threshold(self):
        # Three ticks of slack, so one missed beat (a restart, a deploy) does
        # not read as a dead worker.
        heartbeat = settings.CELERY_BEAT_SCHEDULE["core.heartbeat"]

        self.assertLessEqual(
            heartbeat["schedule"] * 2, settings.CELERY_WORKER_STALE_AFTER
        )

    def test_the_hour_gated_and_nightly_entries_keep_their_minute(self):
        for task_name, minute in EXPECTED_MINUTES.items():
            with self.subTest(task=task_name):
                schedule = settings.CELERY_BEAT_SCHEDULE[task_name]["schedule"]
                self.assertEqual(schedule.minute, {minute})

    def test_the_nightly_purges_do_not_share_a_minute(self):
        # purge_unconsented calls purge_deleted_accounts' own _purge, and the
        # two Places jobs share one daily Google budget.
        nightly = [
            "places.refresh_place_coords",
            "accounts.purge_deleted_accounts",
            "guardians.purge_unconsented",
            "accounts.downgrade_precise_locations",
        ]
        slots = [
            (
                tuple(settings.CELERY_BEAT_SCHEDULE[t]["schedule"].hour),
                tuple(settings.CELERY_BEAT_SCHEDULE[t]["schedule"].minute),
            )
            for t in nightly
        ]

        self.assertEqual(len(set(slots)), len(nightly))

    def test_the_schedule_is_read_in_ist(self):
        # Every crontab above is an India time. Django's TIME_ZONE stays UTC
        # and is deliberately unaffected.
        self.assertEqual(settings.CELERY_TIMEZONE, "Asia/Kolkata")
        self.assertEqual(settings.TIME_ZONE, "UTC")

    def test_the_scheduled_commands_are_all_safe_to_run_twice(self):
        # acks_late is on for every scheduled task, which makes redelivery
        # routine rather than exceptional. Each of these commands recognises
        # its own previous work; the task docstrings say how.
        for entry in settings.CELERY_BEAT_SCHEDULE.values():
            task = celery_app.tasks[entry["task"]]
            if entry["task"] == "core.heartbeat":
                continue   # at-most-once on purpose: see its docstring
            with self.subTest(task=entry["task"]):
                self.assertTrue(task.acks_late)
