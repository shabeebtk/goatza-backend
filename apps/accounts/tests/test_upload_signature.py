"""
The signature endpoint: re-signing into an in-flight batch, and URL lifetimes.

These run a REAL R2Service against fake credentials. Presigning is pure local
computation — botocore never opens a connection to sign a URL — so the URLs
and keys asserted here are exactly what production would hand out, with no
network and no secrets.
"""

import time
import uuid

from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.legal.testing import accept_current_terms
from services.storage.r2 import UPLOAD_URL_EXPIRES_IN

SIGNATURE_URL = "/user/get/upload/signature"

FAKE_R2 = {
    "FILE_STORAGE_PROVIDER": "r2",
    "R2_ACCOUNT_ID": "testaccount",
    "R2_ACCESS_KEY_ID": "test-access-key",
    "R2_SECRET_ACCESS_KEY": "test-secret-key",
    "R2_BUCKET": "test-bucket",
    "MEDIA_PUBLIC_BASE_URL": "https://media.goatza.test",
    "PASSWORD_HASHERS": ["django.contrib.auth.hashers.MD5PasswordHasher"],
}

IMAGE = {"content_type": "image/webp", "size_bytes": 800_000, "kind": "image"}
THUMB = {"content_type": "image/webp", "size_bytes": 40_000, "kind": "thumb"}


@override_settings(**FAKE_R2)
class TempPostIdTests(TestCase):
    """
    `temp_post_id` lets a client re-sign the rest of a batch into the SAME
    folder after its URLs expired or were refused — which is what keeps a
    thumb in its image's folder when the pair straddles two requests.
    """

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            email="uploader@example.com", password="pass1234",
            username="uploader",
        )
        accept_current_terms(self.user)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _sign(self, **body):
        return self.client.post(SIGNATURE_URL, body, format="json")

    def test_a_posts_batch_is_re_signed_into_the_folder_it_names(self):
        temp_id = str(uuid.uuid4())

        resp = self._sign(type="posts", files=[IMAGE, THUMB], temp_post_id=temp_id)

        self.assertEqual(resp.status_code, 200, resp.data)
        data = resp.data["data"]
        self.assertEqual(data["temp_post_id"], temp_id)
        folder = f"users/{self.user.id}/posts/{temp_id}/"
        for upload in data["uploads"]:
            self.assertTrue(upload["key"].startswith(folder), upload["key"])

    def test_without_one_every_request_mints_a_new_folder(self):
        first = self._sign(type="posts", files=[IMAGE]).data["data"]
        second = self._sign(type="posts", files=[IMAGE]).data["data"]

        self.assertNotEqual(first["temp_post_id"], second["temp_post_id"])

    def test_it_is_ignored_for_a_type_with_no_batch_folder(self):
        # Even malformed: the field means nothing for a fixed-slot type, so it
        # cannot be a reason to refuse one.
        resp = self._sign(type="profile", files=[IMAGE], temp_post_id="not-a-uuid")

        self.assertEqual(resp.status_code, 200, resp.data)
        data = resp.data["data"]
        self.assertNotIn("temp_post_id", data)
        self.assertEqual(
            data["uploads"][0]["key"], f"users/{self.user.id}/profile.webp"
        )

    def test_a_malformed_id_is_a_400_on_the_field(self):
        for bad in ["not-a-uuid", "", 12345]:
            with self.subTest(bad=bad):
                resp = self._sign(type="posts", files=[IMAGE], temp_post_id=bad)

                self.assertEqual(resp.status_code, 400, resp.data)
                self.assertIn("temp_post_id", resp.data["data"]["errors"])

    def test_only_the_canonical_spelling_is_accepted(self):
        # uuid.UUID() takes all of these; a folder that can be spelled five
        # ways is five folders.
        canonical = str(uuid.uuid4())
        for spelling in [
            canonical.upper(),
            canonical.replace("-", ""),
            "{" + canonical + "}",
            "urn:uuid:" + canonical,
        ]:
            with self.subTest(spelling=spelling):
                resp = self._sign(type="posts", files=[IMAGE], temp_post_id=spelling)

                self.assertEqual(resp.status_code, 400, resp.data)

    def test_the_folder_is_always_under_the_callers_own_prefix(self):
        # The id names a folder, never an owner: the same id from another
        # user lands under THEIR prefix.
        temp_id = str(uuid.uuid4())
        other = User.objects.create_user(
            email="other@example.com", password="pass1234", username="other",
        )
        accept_current_terms(other)
        self.client.force_authenticate(user=other)

        resp = self._sign(type="posts", files=[IMAGE], temp_post_id=temp_id)

        key = resp.data["data"]["uploads"][0]["key"]
        self.assertTrue(key.startswith(f"users/{other.id}/posts/{temp_id}/"))


@override_settings(**FAKE_R2)
class UrlLifetimeTests(TestCase):

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            email="timer@example.com", password="pass1234", username="timer",
        )
        accept_current_terms(self.user)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_every_entry_carries_an_absolute_expiry_an_hour_out(self):
        before = int(time.time())
        resp = self.client.post(
            SIGNATURE_URL, {"type": "posts", "files": [IMAGE, THUMB]},
            format="json",
        )
        after = int(time.time())

        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(UPLOAD_URL_EXPIRES_IN, 3600)
        for upload in resp.data["data"]["uploads"]:
            self.assertEqual(upload["expires_in"], 3600)
            self.assertGreaterEqual(upload["expires_at"], before + 3600)
            self.assertLessEqual(upload["expires_at"], after + 3600)
            self.assertIn("X-Amz-Expires=3600", upload["upload_url"])
