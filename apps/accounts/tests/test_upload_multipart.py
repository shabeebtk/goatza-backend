"""
The multipart endpoints, against a mocked storage service.

What matters here is the gatekeeping — whose key, which type, how big, which
parts — so the storage service is a mock and every test asserts what reached
it (or that nothing did). The R2 calls themselves are covered in
shared/test_storage_r2.py.
"""

import uuid
from unittest.mock import MagicMock, patch

from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.accounts.views.user_upload_multipart_views import MULTIPART_PART_SIZE
from apps.accounts.views.user_upload_signature_views import MB
from apps.legal.testing import accept_current_terms
from apps.organization.models import Organization, OrganizationMember

CREATE_URL = "/user/upload/multipart/create"
COMPLETE_URL = "/user/upload/multipart/complete"
ABORT_URL = "/user/upload/multipart/abort"

STORAGE = "apps.accounts.views.user_upload_multipart_views.get_storage_service"


def fake_storage():
    storage = MagicMock()
    storage.create_multipart_upload.return_value = "upload-123"
    # A realistic part URL, signature and all — none of it may ever be logged.
    storage.presign_upload_part.side_effect = (
        lambda key, upload_id, number:
        f"https://r2.test/{key}?partNumber={number}&uploadId={upload_id}"
        f"&X-Amz-Signature=secret"
    )
    return storage


@override_settings(
    MEDIA_PUBLIC_BASE_URL="https://media.goatza.test",
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class MultipartTestCase(TestCase):

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            email="player@example.com", password="pass1234", username="player",
        )
        accept_current_terms(self.user)
        self.other = User.objects.create_user(
            email="rival@example.com", password="pass1234", username="rival",
        )
        accept_current_terms(self.other)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _post_key(self, user=None, ext="mp4"):
        owner = user or self.user
        return f"users/{owner.id}/posts/{uuid.uuid4()}/{uuid.uuid4()}.{ext}"

    def _create(self, storage, **overrides):
        body = {
            "type": "posts",
            "key": self._post_key(),
            "content_type": "video/mp4",
            "size_bytes": 20 * MB,
        }
        body.update(overrides)
        with patch(STORAGE, return_value=storage):
            return self.client.post(CREATE_URL, body, format="json")


class CreateTests(MultipartTestCase):

    def test_happy_path_signs_one_url_per_part(self):
        storage = fake_storage()
        key = self._post_key()

        resp = self._create(storage, key=key, size_bytes=20 * MB)

        self.assertEqual(resp.status_code, 200, resp.data)
        data = resp.data["data"]
        self.assertEqual(data["upload_id"], "upload-123")
        self.assertEqual(data["key"], key)
        self.assertEqual(data["public_url"], f"https://media.goatza.test/{key}")
        self.assertEqual(data["part_size"], MULTIPART_PART_SIZE)
        self.assertEqual(MULTIPART_PART_SIZE, 8 * MB)
        # 20 MB in 8 MB parts: 8 + 8 + 4.
        self.assertEqual([p["part_number"] for p in data["parts"]], [1, 2, 3])
        self.assertIn("expires_at", data)
        self.assertEqual(data["expires_in"], 3600)
        storage.create_multipart_upload.assert_called_once_with(key, "video/mp4")
        self.assertEqual(storage.presign_upload_part.call_count, 3)

    def test_an_upload_id_re_signs_the_same_upload_instead_of_starting_one(self):
        storage = fake_storage()
        key = self._post_key()

        resp = self._create(storage, key=key, upload_id="existing-upload")

        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data["data"]["upload_id"], "existing-upload")
        storage.create_multipart_upload.assert_not_called()
        for call in storage.presign_upload_part.call_args_list:
            self.assertEqual(call.args[:2], (key, "existing-upload"))

    def test_a_key_under_another_users_prefix_is_refused(self):
        storage = fake_storage()

        resp = self._create(storage, key=self._post_key(user=self.other))

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("key", resp.data["data"]["errors"])
        storage.create_multipart_upload.assert_not_called()

    def test_a_key_outside_the_types_folder_is_refused(self):
        # The caller's own prefix, but a highlights folder claimed as posts:
        # it would borrow the posts size cap.
        storage = fake_storage()
        key = f"users/{self.user.id}/highlights/{uuid.uuid4()}.mp4"

        resp = self._create(storage, key=key)

        self.assertEqual(resp.status_code, 400, resp.data)
        storage.create_multipart_upload.assert_not_called()

    def test_a_key_that_is_not_one_we_would_issue_is_refused(self):
        storage = fake_storage()
        prefix = f"users/{self.user.id}/posts"
        for key in [
            f"{prefix}/not-a-uuid/{uuid.uuid4()}.mp4",
            f"{prefix}/{uuid.uuid4()}/clip.mp4",
            f"{prefix}/{uuid.uuid4()}/nested/{uuid.uuid4()}.mp4",
            f"{prefix}/{uuid.uuid4()}.mp4",
            "",
            None,
        ]:
            with self.subTest(key=key):
                resp = self._create(storage, key=key)
                self.assertEqual(resp.status_code, 400, resp.data)

        storage.create_multipart_upload.assert_not_called()

    def test_a_size_over_the_types_cap_is_refused(self):
        storage = fake_storage()

        resp = self._create(storage, size_bytes=80 * MB + 1)

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("size_bytes", resp.data["data"]["errors"])
        storage.create_multipart_upload.assert_not_called()

    def test_a_junk_size_is_refused(self):
        storage = fake_storage()
        for size in [0, -1, True, "20000000", 1.5, None]:
            with self.subTest(size=size):
                resp = self._create(storage, size_bytes=size)
                self.assertEqual(resp.status_code, 400, resp.data)

    def test_a_non_video_content_type_is_refused(self):
        storage = fake_storage()

        resp = self._create(storage, content_type="image/webp")

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("content_type", resp.data["data"]["errors"])
        storage.create_multipart_upload.assert_not_called()

    def test_the_key_extension_must_match_the_content_type(self):
        storage = fake_storage()

        resp = self._create(storage, key=self._post_key(ext="webm"))

        self.assertEqual(resp.status_code, 400, resp.data)
        storage.create_multipart_upload.assert_not_called()

    def test_a_type_that_carries_no_video_is_refused(self):
        storage = fake_storage()
        for upload_type in ["profile", "achievements", "support", "nonsense"]:
            with self.subTest(upload_type=upload_type):
                resp = self._create(storage, type=upload_type)
                self.assertEqual(resp.status_code, 400, resp.data)
                self.assertIn("type", resp.data["data"]["errors"])

    def test_a_chat_video_key_is_accepted(self):
        # Chat keys live under chat/users/<id>/, OUTSIDE users/<id>/ — the
        # reason this cannot be a plain validate_public_id prefix check.
        storage = fake_storage()
        key = f"chat/users/{self.user.id}/{uuid.uuid4()}.mp4"

        resp = self._create(storage, type="chat", key=key)

        self.assertEqual(resp.status_code, 200, resp.data)
        storage.create_multipart_upload.assert_called_once_with(key, "video/mp4")

    def test_an_org_actor_signs_into_its_own_folder(self):
        org = Organization.objects.create(
            name="Dream FC", username="dreamfc", type=Organization.Type.CLUB,
        )
        OrganizationMember.objects.create(
            organization=org, user=self.user,
            role=OrganizationMember.Role.OWNER,
        )
        storage = fake_storage()
        key = f"organizations/{org.id}/posts/{uuid.uuid4()}/{uuid.uuid4()}.mp4"

        with patch(STORAGE, return_value=storage):
            resp = self.client.post(
                CREATE_URL,
                {"type": "posts", "key": key, "content_type": "video/mp4",
                 "size_bytes": 20 * MB},
                format="json",
                HTTP_X_ACTOR_TYPE="organization",
                HTTP_X_ACTOR_ID=str(org.id),
            )

        self.assertEqual(resp.status_code, 200, resp.data)

    def test_no_part_url_reaches_the_log_on_a_storage_failure(self):
        storage = fake_storage()
        storage.create_multipart_upload.side_effect = RuntimeError("R2 is down")

        with self.assertLogs(
            "apps.accounts.views.user_upload_multipart_views", level="ERROR"
        ) as logs:
            resp = self._create(storage)

        self.assertEqual(resp.status_code, 500)
        self.assertNotIn("X-Amz", "".join(logs.output))


class CompleteTests(MultipartTestCase):

    def _complete(self, storage, **body):
        with patch(STORAGE, return_value=storage):
            return self.client.post(COMPLETE_URL, body, format="json")

    def test_parts_are_sorted_and_etags_passed_through_untouched(self):
        storage = fake_storage()
        key = self._post_key()

        resp = self._complete(
            storage, key=key, upload_id="upload-123",
            parts=[
                {"part_number": 2, "etag": '"bbb"'},
                {"part_number": 1, "etag": '"aaa"'},
                {"part_number": 3, "etag": '"ccc"'},
            ],
        )

        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data["data"]["key"], key)
        self.assertEqual(
            resp.data["data"]["public_url"], f"https://media.goatza.test/{key}"
        )
        storage.complete_multipart_upload.assert_called_once_with(
            key, "upload-123",
            [
                {"part_number": 1, "etag": '"aaa"'},
                {"part_number": 2, "etag": '"bbb"'},
                {"part_number": 3, "etag": '"ccc"'},
            ],
        )

    def test_non_contiguous_parts_are_refused(self):
        storage = fake_storage()
        for parts in [
            [{"part_number": 1, "etag": '"a"'}, {"part_number": 3, "etag": '"c"'}],
            [{"part_number": 2, "etag": '"b"'}],
            [{"part_number": 1, "etag": '"a"'}, {"part_number": 1, "etag": '"a"'}],
            [{"part_number": 0, "etag": '"a"'}],
            [],
        ]:
            with self.subTest(parts=parts):
                resp = self._complete(
                    storage, key=self._post_key(), upload_id="upload-123",
                    parts=parts,
                )
                self.assertEqual(resp.status_code, 400, resp.data)
                self.assertIn("parts", resp.data["data"]["errors"])

        storage.complete_multipart_upload.assert_not_called()

    def test_a_part_without_its_etag_is_refused(self):
        storage = fake_storage()
        for etag in ["", "   ", None, 7]:
            with self.subTest(etag=etag):
                resp = self._complete(
                    storage, key=self._post_key(), upload_id="upload-123",
                    parts=[{"part_number": 1, "etag": etag}],
                )
                self.assertEqual(resp.status_code, 400, resp.data)

        storage.complete_multipart_upload.assert_not_called()

    def test_another_users_key_is_refused(self):
        storage = fake_storage()

        resp = self._complete(
            storage, key=self._post_key(user=self.other), upload_id="upload-123",
            parts=[{"part_number": 1, "etag": '"a"'}],
        )

        self.assertEqual(resp.status_code, 400, resp.data)
        storage.complete_multipart_upload.assert_not_called()

    def test_a_missing_upload_id_is_refused(self):
        storage = fake_storage()

        resp = self._complete(
            storage, key=self._post_key(),
            parts=[{"part_number": 1, "etag": '"a"'}],
        )

        self.assertEqual(resp.status_code, 400, resp.data)
        storage.complete_multipart_upload.assert_not_called()


class AbortTests(MultipartTestCase):

    def _abort(self, storage, **body):
        with patch(STORAGE, return_value=storage):
            return self.client.post(ABORT_URL, body, format="json")

    def test_abort_reaches_storage(self):
        storage = fake_storage()
        key = self._post_key()

        resp = self._abort(storage, key=key, upload_id="upload-123")

        self.assertEqual(resp.status_code, 200, resp.data)
        storage.abort_multipart_upload.assert_called_once_with(key, "upload-123")

    def test_a_chat_key_can_be_aborted_by_its_owner(self):
        storage = fake_storage()
        key = f"chat/users/{self.user.id}/{uuid.uuid4()}.mp4"

        resp = self._abort(storage, key=key, upload_id="upload-123")

        self.assertEqual(resp.status_code, 200, resp.data)

    def test_another_users_key_is_refused(self):
        storage = fake_storage()

        resp = self._abort(
            storage, key=self._post_key(user=self.other), upload_id="upload-123",
        )

        self.assertEqual(resp.status_code, 400, resp.data)
        storage.abort_multipart_upload.assert_not_called()
