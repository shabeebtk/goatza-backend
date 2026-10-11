"""
R2 deletes run in a worker now, and "best-effort" no longer means "orphaned".

WHAT THESE REPLACE. Every delete of a stored object was an inline call in a
``transaction.on_commit`` callback — which runs in the REQUEST's thread, after
the response status is decided but before the worker is free. A slow R2 was
time the user spent waiting on a file they had already been told was deleted,
and any failure left the object billed forever with one log line as its
gravestone.

The retry policy is the OPPOSITE of the push and email tasks on purpose: a
delete IS idempotent (removing a key that is already gone is a no-op on R2),
so these are at-least-once with ``acks_late=True`` and five backed-off
retries. The final attempt names every key at ERROR, because by then nothing
else in the system knows those objects exist — the rows that referenced them
are gone.
"""

import uuid
from unittest.mock import patch

from django.test import TestCase, override_settings

from apps.accounts.models import User, UserProfile
from apps.legal.testing import accept_current_terms
from apps.posts.models import Post, PostMedia
from apps.posts.services.post_service import PostService
from core.actor import Actor
from shared.tasks import delete_folder, delete_keys

STORAGE = "shared.tasks.get_storage_service"


class DeleteKeysTaskTests(TestCase):

    def test_each_key_is_deleted(self):
        with patch(STORAGE) as get_storage:
            delete_keys(["users/a/profile.jpg", "users/a/cover.jpg"])

        self.assertEqual(
            [call.args for call in get_storage.return_value.delete_file.call_args_list],
            [("users/a/profile.jpg",), ("users/a/cover.jpg",)],
        )

    def test_an_empty_list_never_reaches_storage(self):
        # The normal case for a caller that found nothing orphaned.
        with patch(STORAGE) as get_storage:
            delete_keys([])
            delete_keys(None)
            delete_keys(["", None])

        get_storage.assert_not_called()

    def test_a_failure_propagates_so_celery_can_retry(self):
        # R2 being unreachable must mean "later", never "never".
        with patch(STORAGE, side_effect=RuntimeError("no credentials")):
            with self.assertRaises(RuntimeError):
                delete_keys(["users/a/profile.jpg"])

    def test_an_early_attempt_does_not_announce_an_orphan(self):
        # There are four more attempts coming; an ERROR here would be a Sentry
        # event for a delete that is about to succeed.
        delete_keys.push_request(retries=0)
        try:
            with patch(STORAGE, side_effect=RuntimeError("blip")):
                with patch("shared.tasks.logger") as logger:
                    with self.assertRaises(RuntimeError):
                        delete_keys(["users/a/profile.jpg"])
        finally:
            delete_keys.pop_request()

        logger.error.assert_not_called()

    def test_the_last_attempt_names_every_orphaned_key_at_error(self):
        keys = ["users/a/profile.jpg", "users/a/cover.jpg"]

        delete_keys.push_request(retries=delete_keys.max_retries)
        try:
            with patch(STORAGE, side_effect=RuntimeError("still gone")):
                with self.assertLogs("shared.tasks", level="ERROR") as logs:
                    with self.assertRaises(RuntimeError):
                        delete_keys(keys)
        finally:
            delete_keys.pop_request()

        self.assertIn("GIVING UP", logs.output[0])
        for key in keys:
            self.assertIn(key, logs.output[0])

    def test_the_retry_policy_is_at_least_once_with_backoff(self):
        self.assertTrue(delete_keys.acks_late)
        self.assertEqual(delete_keys.max_retries, 5)
        self.assertEqual(delete_keys.retry_backoff_max, 600)


class DeleteFolderTaskTests(TestCase):

    def test_the_prefix_is_swept(self):
        with patch(STORAGE) as get_storage:
            delete_folder("posts/abc")

        get_storage.return_value.delete_folder_data.assert_called_once_with(
            "posts/abc"
        )

    def test_no_prefix_never_reaches_storage(self):
        with patch(STORAGE) as get_storage:
            delete_folder("")

        get_storage.assert_not_called()

    def test_the_last_attempt_names_the_orphaned_prefix(self):
        delete_folder.push_request(retries=delete_folder.max_retries)
        try:
            with patch(STORAGE, side_effect=RuntimeError("gone")):
                with self.assertLogs("shared.tasks", level="ERROR") as logs:
                    with self.assertRaises(RuntimeError):
                        delete_folder("posts/abc")
        finally:
            delete_folder.pop_request()

        self.assertIn("prefix=posts/abc", logs.output[0])


@override_settings(
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"]
)
class PostDeleteCallSiteTests(TestCase):
    """
    Deleting a post sweeps its media folder — AFTER the transaction commits.

    That ordering is a fix, not a preservation: the sweep used to run INSIDE
    the atomic block, so a transaction that rolled back afterwards left the
    post in the database with its media already gone from R2.
    """

    def setUp(self):
        self.author = User.objects.create_user(
            email="author@example.com", password="pass1234", username="author",
        )
        accept_current_terms(self.author)
        UserProfile.objects.create(user=self.author, name="Author")

        self.post = Post.objects.create(author_user=self.author, content="hello")
        self.folder = f"users/{self.author.id}/posts/{uuid.uuid4()}"
        PostMedia.objects.create(
            post=self.post,
            file_url=f"https://media.goatza.com/{self.folder}/1.jpg",
            public_id=f"{self.folder}/1.jpg",
            media_type="image",
        )

    def _actor(self):
        return Actor(actor_type="user", user=self.author)

    def test_the_folder_is_handed_to_the_task_as_a_prefix(self):
        with patch("apps.posts.services.post_service.enqueue") as enqueue:
            ok, _ = PostService.delete_post(str(self.post.id), self._actor())

        self.assertTrue(ok)
        enqueue.assert_called_once()
        task, args = enqueue.call_args.args
        self.assertEqual(task.name, "storage.delete_folder")
        self.assertEqual(args, (self.folder,))

    def test_the_sweep_waits_for_the_commit(self):
        with patch(STORAGE) as get_storage:
            with self.captureOnCommitCallbacks(execute=False) as callbacks:
                PostService.delete_post(str(self.post.id), self._actor())
                # Registered, not run: the post row is not committed yet.
                get_storage.assert_not_called()

            self.assertEqual(len(callbacks), 1)
            callbacks[0]()

        get_storage.return_value.delete_folder_data.assert_called_once_with(
            self.folder
        )

    def test_a_post_with_no_media_sweeps_nothing(self):
        bare = Post.objects.create(author_user=self.author, content="no media")

        with patch("apps.posts.services.post_service.enqueue") as enqueue:
            PostService.delete_post(str(bare.id), self._actor())

        enqueue.assert_not_called()


@override_settings(
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"]
)
class RecruitmentOrphanCallSiteTests(TestCase):
    """
    Editing a recruitment's media hands the keys it orphaned to the task.

    Driven through ``_sync_media`` rather than the update endpoint on purpose:
    ``RecruitmentMediaPipelineTests`` already owns the HTTP-level version, and
    the dispatch under test here is a property of the sync helper — reaching it
    through the API would make this test fail for every unrelated change to the
    recruitment payload.
    """

    def setUp(self):
        from apps.organization.models import Organization
        from apps.recruitments.models import Recruitment, RecruitmentMedia
        from apps.sports.models import Sport

        self.Media = RecruitmentMedia
        self.org = Organization.objects.create(
            name="Dream FC", username="dreamfc", type=Organization.Type.CLUB,
        )
        self.sport = Sport.objects.create(name="Football", icon_name="mdi:soccer")
        self.recruitment = Recruitment.objects.create(
            organization=self.org,
            sport=self.sport,
            title="U18 Trials",
            recruitment_type=Recruitment.Type.OPEN_TRIAL,
        )

        self.keep = f"organizations/{self.org.id}/recruitments/{uuid.uuid4()}/keep"
        self.orphan = f"organizations/{self.org.id}/recruitments/{uuid.uuid4()}/drop"
        for index, public_id in enumerate((self.keep, self.orphan)):
            RecruitmentMedia.objects.create(
                recruitment=self.recruitment,
                file_url=f"https://media.goatza.com/{public_id}.jpg",
                public_id=public_id,
                media_type="image",
                order=index,
            )

    def _incoming(self, *public_ids):
        return [
            {
                "file_url": f"https://media.goatza.com/{public_id}.jpg",
                "public_id": public_id,
                "media_type": "image",
                "order": index,
            }
            for index, public_id in enumerate(public_ids)
        ]

    def _sync(self, *public_ids):
        from apps.recruitments.services.recruitment_service import RecruitmentService

        return RecruitmentService._sync_media(
            self.recruitment, self._incoming(*public_ids)
        )

    def test_the_dropped_key_is_handed_to_the_task(self):
        with patch(
            "apps.recruitments.services.recruitment_service.enqueue"
        ) as enqueue:
            self._sync(self.keep)

        enqueue.assert_called_once()
        task, args = enqueue.call_args.args
        self.assertEqual(task.name, "storage.delete_keys")
        self.assertEqual(args, ([self.orphan],))

    def test_keeping_everything_dispatches_nothing(self):
        with patch(
            "apps.recruitments.services.recruitment_service.enqueue"
        ) as enqueue:
            self._sync(self.keep, self.orphan)

        enqueue.assert_not_called()

    def test_the_delete_waits_for_the_commit(self):
        # Files are never destroyed for an edit that rolled back. enqueue's
        # on_commit default IS that guarantee, and this is what pins it.
        with patch(STORAGE) as get_storage:
            with self.captureOnCommitCallbacks(execute=False) as callbacks:
                self._sync(self.keep)
                get_storage.assert_not_called()

            self.assertEqual(len(callbacks), 1)
            callbacks[0]()

        get_storage.return_value.delete_file.assert_called_once_with(self.orphan)
