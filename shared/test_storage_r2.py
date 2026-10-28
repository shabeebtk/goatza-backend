"""
R2Service and the r2_setup command.

Two kinds of client here, on purpose:

- A REAL botocore client with fake credentials for anything about a presigned
  URL. Signing is local computation — nothing is sent anywhere — so the
  assertions are about the exact URL production would hand a browser.
- A mocked boto3 client for the bucket-configuration and multipart calls,
  which WOULD talk to R2. The mock is the record of what was sent, and of
  what was never sent: the lifecycle tests are as much about the calls that
  must not happen as the ones that must.
"""

import io
import time
import uuid
from unittest.mock import MagicMock, patch
from urllib.parse import urlsplit

from botocore.exceptions import ClientError
from django.core.exceptions import ImproperlyConfigured
from django.core.management import CommandError, call_command
from django.test import SimpleTestCase, override_settings

from core.actor import Actor
from core.settings import upload_gateway_url
from services.storage.r2 import (
    UPLOAD_URL_EXPIRES_IN,
    R2Service,
    browser_upload_url,
)

FAKE_R2 = {
    "R2_ACCOUNT_ID": "testaccount",
    "R2_ACCESS_KEY_ID": "test-access-key",
    "R2_SECRET_ACCESS_KEY": "test-secret-key",
    "R2_BUCKET": "test-bucket",
    "MEDIA_PUBLIC_BASE_URL": "https://media.goatza.test",
    # Direct unless a test says otherwise, whatever the local .env holds.
    "R2_UPLOAD_GATEWAY_URL": "",
}

S3_ORIGIN = "https://testaccount.r2.cloudflarestorage.com"
GATEWAY = "https://upload.goatza.test"

BOTO_CLIENT = "services.storage.r2.boto3.client"
COMMAND_STORAGE = "shared.management.commands.r2_setup.get_storage_service"


class FakeUser:
    id = "0190aaaa-bbbb-7ccc-8ddd-eeeeeeeeeeee"


def user_actor():
    return Actor(actor_type="user", user=FakeUser())


def client_error(code, operation):
    return ClientError({"Error": {"Code": code, "Message": code}}, operation)


IMAGE = {"content_type": "image/webp", "size_bytes": 1000, "kind": "image"}
THUMB = {"content_type": "image/webp", "size_bytes": 100, "kind": "thumb"}


@override_settings(**FAKE_R2)
class PresignTests(SimpleTestCase):

    def test_content_type_and_host_are_both_signed(self):
        # The Content-Type is IN the signature — a browser that sends another
        # type, or a client that declared image/webp and PUTs a video, is
        # refused by R2 itself.
        config = R2Service().get_upload_config(user_actor(), "posts", [IMAGE])

        url = config["uploads"][0]["upload_url"]
        self.assertIn("X-Amz-SignedHeaders=content-type%3Bhost", url)
        self.assertIn("X-Amz-Expires=3600", url)

    def test_a_temp_id_reuses_its_batch_folder(self):
        temp_id = str(uuid.uuid4())

        config = R2Service().get_upload_config(
            user_actor(), "posts", [IMAGE, THUMB], temp_id=temp_id
        )

        self.assertEqual(config["temp_post_id"], temp_id)
        for upload in config["uploads"]:
            self.assertTrue(
                upload["key"].startswith(f"users/{FakeUser.id}/posts/{temp_id}/")
            )

    def test_without_a_temp_id_a_new_folder_is_minted(self):
        service = R2Service()
        first = service.get_upload_config(user_actor(), "posts", [IMAGE])
        second = service.get_upload_config(user_actor(), "posts", [IMAGE])

        self.assertNotEqual(first["temp_post_id"], second["temp_post_id"])

    def test_a_temp_id_means_nothing_to_a_type_without_batches(self):
        config = R2Service().get_upload_config(
            user_actor(), "highlights", [IMAGE], temp_id=str(uuid.uuid4())
        )

        self.assertNotIn("temp_post_id", config)
        self.assertTrue(
            config["uploads"][0]["key"].startswith(
                f"users/{FakeUser.id}/highlights/"
            )
        )

    def test_entries_carry_an_absolute_expiry(self):
        before = int(time.time())
        entry = R2Service().get_upload_config(user_actor(), "posts", [IMAGE])[
            "uploads"
        ][0]

        self.assertEqual(entry["expires_in"], UPLOAD_URL_EXPIRES_IN)
        self.assertGreaterEqual(entry["expires_at"], before + 3600)
        self.assertLessEqual(entry["expires_at"], int(time.time()) + 3600)

    def test_a_part_url_signs_host_only(self):
        # The browser sends no header on a part — so none may be signed.
        url = R2Service().presign_upload_part(
            "users/1/posts/x/y.mp4", "upload-123", 2
        )

        self.assertIn("partNumber=2", url)
        self.assertIn("uploadId=upload-123", url)
        self.assertIn("X-Amz-SignedHeaders=host", url)
        self.assertIn("X-Amz-Expires=3600", url)


@override_settings(**FAKE_R2)
class UploadGatewayTests(SimpleTestCase):
    """
    R2_UPLOAD_GATEWAY_URL moves the host a browser uploads to, and nothing
    else. Each test signs for real and records the URL botocore produced, so
    the comparison is against the actual signature, not a reconstruction —
    the gateway forwards path + query unchanged, and R2 only accepts them if
    they are exactly what was signed.
    """

    def _signed_and_handed_out(self, call):
        """(the URL botocore signed, the URL the browser is handed)."""
        service = R2Service()
        sign = service.client.generate_presigned_url
        signed = []

        def record(*args, **kwargs):
            url = sign(*args, **kwargs)
            signed.append(url)
            return url

        with patch.object(
            service.client, "generate_presigned_url", side_effect=record
        ):
            handed_out = call(service)

        self.assertEqual(len(signed), 1)
        return signed[0], handed_out

    @staticmethod
    def _put(service):
        return service._build_presigned_put(
            key="users/1/posts/x/y.webp", content_type="image/webp"
        )["upload_url"]

    @staticmethod
    def _part(service):
        return service.presign_upload_part(
            "users/1/posts/x/y.mp4", "upload-123", 2
        )

    def _assert_only_the_host_moved(self, signed, handed_out):
        # Path-style: the account is the host and the bucket is in the path.
        # The gateway forwards to that one host and refuses any other bucket.
        self.assertTrue(signed.startswith(f"{S3_ORIGIN}/test-bucket/"))

        before, after = urlsplit(signed), urlsplit(handed_out)
        self.assertEqual(after.scheme, "https")
        self.assertEqual(after.netloc, "upload.goatza.test")
        self.assertEqual(after.path, before.path)
        # The signature, byte for byte.
        self.assertEqual(after.query, before.query)
        self.assertIn("X-Amz-Signature=", after.query)
        self.assertEqual(handed_out, GATEWAY + signed[len(S3_ORIGIN):])

    def test_without_a_gateway_the_signed_url_is_handed_out_unchanged(self):
        for call in (self._put, self._part):
            with self.subTest(call=call.__name__):
                signed, handed_out = self._signed_and_handed_out(call)

                self.assertEqual(handed_out, signed)
                self.assertTrue(handed_out.startswith(f"{S3_ORIGIN}/test-bucket/"))

    @override_settings(R2_UPLOAD_GATEWAY_URL=GATEWAY)
    def test_a_whole_object_put_moves_to_the_gateway_host_only(self):
        signed, handed_out = self._signed_and_handed_out(self._put)

        self._assert_only_the_host_moved(signed, handed_out)
        self.assertIn(
            "X-Amz-SignedHeaders=content-type%3Bhost", urlsplit(handed_out).query
        )

    @override_settings(R2_UPLOAD_GATEWAY_URL=GATEWAY)
    def test_a_part_moves_to_the_gateway_host_only(self):
        signed, handed_out = self._signed_and_handed_out(self._part)

        self._assert_only_the_host_moved(signed, handed_out)
        query = urlsplit(handed_out).query
        self.assertIn("partNumber=2", query)
        self.assertIn("uploadId=upload-123", query)
        self.assertIn("X-Amz-SignedHeaders=host", query)

    @override_settings(R2_UPLOAD_GATEWAY_URL=GATEWAY)
    def test_the_query_is_never_decoded_or_re_encoded(self):
        signed = (
            f"{S3_ORIGIN}/test-bucket/users/1/a%20b.webp"
            "?uploadId=a~b%2Bc+d&partNumber=1"
            "&X-Amz-Credential=k%2F20261028%2Fauto%2Fs3%2Faws4_request"
            "&X-Amz-SignedHeaders=host&X-Amz-Signature=00ff"
        )

        self.assertEqual(
            browser_upload_url(signed), GATEWAY + signed[len(S3_ORIGIN):]
        )


class UploadGatewaySettingTests(SimpleTestCase):
    """settings.upload_gateway_url — what the app refuses to start with."""

    def test_empty_means_direct(self):
        self.assertEqual(upload_gateway_url(""), "")

    def test_a_bare_https_origin_is_accepted(self):
        for value in ("https://upload.goatza.com", "https://upload.goatza.com:8443"):
            with self.subTest(value=value):
                self.assertEqual(upload_gateway_url(value), value)

    def test_anything_else_is_refused(self):
        for value in (
            "http://upload.goatza.com",       # the signed query in clear text
            "https://upload.goatza.com/r2",   # a path would be silently dropped
            "https://upload.goatza.com?x=1",
            "https://upload.goatza.com#top",
            "upload.goatza.com",
            "https://user@upload.goatza.com",
            "https://",
        ):
            with self.subTest(value=value):
                with self.assertRaises(ImproperlyConfigured):
                    upload_gateway_url(value)


@override_settings(**FAKE_R2)
class BucketConfigTests(SimpleTestCase):

    def setUp(self):
        patcher = patch(BOTO_CLIENT)
        self.boto = patcher.start().return_value
        self.addCleanup(patcher.stop)

    def test_no_cors_configuration_reads_as_an_empty_list(self):
        self.boto.get_bucket_cors.side_effect = client_error(
            "NoSuchCORSConfiguration", "GetBucketCors"
        )

        self.assertEqual(R2Service().get_bucket_cors(), [])

    def test_any_other_cors_error_is_raised(self):
        # An operator asking "what is configured?" must never be told
        # "nothing" because the question failed.
        self.boto.get_bucket_cors.side_effect = client_error(
            "AccessDenied", "GetBucketCors"
        )

        with self.assertRaises(ClientError):
            R2Service().get_bucket_cors()

    def test_no_lifecycle_configuration_reads_as_an_empty_list(self):
        self.boto.get_bucket_lifecycle_configuration.side_effect = client_error(
            "NoSuchLifecycleConfiguration", "GetBucketLifecycleConfiguration"
        )

        self.assertEqual(R2Service().get_bucket_lifecycle_rules(), [])

    def test_put_bucket_cors_replaces_the_policy(self):
        rules = [{"AllowedOrigins": ["https://goatza.com"]}]

        R2Service().put_bucket_cors(rules)

        self.boto.put_bucket_cors.assert_called_once_with(
            Bucket="test-bucket", CORSConfiguration={"CORSRules": rules}
        )

    def test_etags_reach_completion_exactly_as_given(self):
        R2Service().complete_multipart_upload(
            "k", "u", [{"part_number": 1, "etag": '"abc"'}]
        )

        self.boto.complete_multipart_upload.assert_called_once_with(
            Bucket="test-bucket", Key="k", UploadId="u",
            MultipartUpload={"Parts": [{"PartNumber": 1, "ETag": '"abc"'}]},
        )

    def test_create_binds_the_content_type(self):
        self.boto.create_multipart_upload.return_value = {"UploadId": "u-1"}

        upload_id = R2Service().create_multipart_upload("k.mp4", "video/mp4")

        self.assertEqual(upload_id, "u-1")
        self.boto.create_multipart_upload.assert_called_once_with(
            Bucket="test-bucket", Key="k.mp4", ContentType="video/mp4"
        )

    def test_abort_is_best_effort(self):
        self.boto.abort_multipart_upload.side_effect = RuntimeError("R2 down")

        with self.assertLogs("services.storage.r2", level="ERROR"):
            R2Service().abort_multipart_upload("k", "u")  # does not raise


@override_settings(
    CORS_ALLOWED_ORIGINS=["https://goatza.com", "https://www.goatza.com"],
    R2_CORS_EXTRA_ORIGINS=["http://localhost:3000", "https://goatza.com/"],
    **FAKE_R2,
)
class R2SetupCommandTests(SimpleTestCase):

    EXPECTED_RULE = {
        "AllowedOrigins": [
            "https://goatza.com",
            "https://www.goatza.com",
            "http://localhost:3000",
        ],
        "AllowedMethods": ["GET", "HEAD", "PUT"],
        "AllowedHeaders": ["Content-Type"],
        "ExposeHeaders": ["ETag"],
        "MaxAgeSeconds": 3600,
    }

    def setUp(self):
        patcher = patch(BOTO_CLIENT)
        self.boto = patcher.start().return_value
        self.addCleanup(patcher.stop)
        self.boto.get_bucket_cors.return_value = {"CORSRules": []}
        self.boto.get_bucket_lifecycle_configuration.return_value = {"Rules": []}

    def _run(self, *args):
        out = io.StringIO()
        call_command("r2_setup", *args, stdout=out)
        return out.getvalue()

    def _lifecycle_writes(self):
        return [
            call for call in self.boto.method_calls
            if "lifecycle" in call[0] and not call[0].startswith("get_")
        ]

    def test_show_is_the_default_and_writes_nothing(self):
        output = self._run()

        self.boto.head_bucket.assert_called_once_with(Bucket="test-bucket")
        self.boto.put_bucket_cors.assert_not_called()
        self.assertEqual(self._lifecycle_writes(), [])
        self.assertIn("testaccount.r2.cloudflarestorage.com", output)
        self.assertIn("test-bucket", output)
        self.assertIn('"ETag"', output)  # the rules --apply would write

    def test_show_says_uploads_go_direct_without_a_gateway(self):
        output = self._run()

        self.assertIn("uploads via   direct", output)
        self.assertNotIn("answers CORS itself", output)

    @override_settings(R2_UPLOAD_GATEWAY_URL=GATEWAY)
    def test_show_names_the_gateway_and_says_bucket_cors_is_moot(self):
        output = self._run()

        self.assertIn(f"uploads via   {GATEWAY} (gateway)", output)
        self.assertIn("answers CORS itself", output)

    def test_show_never_prints_a_secret(self):
        output = self._run("--show")

        self.assertNotIn("test-secret-key", output)
        self.assertNotIn("test-access-key", output)

    def test_apply_writes_exactly_the_one_rule(self):
        self._run("--apply")

        self.boto.put_bucket_cors.assert_called_once_with(
            Bucket="test-bucket",
            CORSConfiguration={"CORSRules": [self.EXPECTED_RULE]},
        )

    def test_apply_never_touches_lifecycle(self):
        self._run("--apply")

        self.assertEqual(self._lifecycle_writes(), [])
        self.boto.get_bucket_lifecycle_configuration.assert_not_called()

    @override_settings(CORS_ALLOWED_ORIGINS=["*"], R2_CORS_EXTRA_ORIGINS=[])
    def test_apply_refuses_a_wildcard(self):
        with self.assertRaises(CommandError):
            self._run("--apply")

        self.boto.put_bucket_cors.assert_not_called()

    @override_settings(CORS_ALLOWED_ORIGINS=[], R2_CORS_EXTRA_ORIGINS=[])
    def test_apply_refuses_an_empty_origin_list(self):
        with self.assertRaises(CommandError):
            self._run("--apply")

        self.boto.put_bucket_cors.assert_not_called()

    def test_an_unreachable_bucket_is_an_error_not_an_empty_answer(self):
        self.boto.head_bucket.side_effect = client_error("403", "HeadBucket")

        with self.assertRaises(CommandError):
            self._run()

        self.boto.put_bucket_cors.assert_not_called()

    def test_the_command_goes_through_the_storage_factory(self):
        # So a deployment with a bad FILE_STORAGE_PROVIDER fails here too.
        storage = MagicMock()
        storage.get_bucket_cors.return_value = []
        storage.get_bucket_lifecycle_rules.return_value = []

        with patch(COMMAND_STORAGE, return_value=storage):
            self._run()

        storage.head_bucket.assert_called_once_with()
        storage.put_bucket_cors.assert_not_called()
