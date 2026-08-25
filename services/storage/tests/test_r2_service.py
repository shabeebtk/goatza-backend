"""
R2Service unit tests (GOATZA_R2_MIGRATION.md §5.1).

boto3 is mocked throughout — nothing here touches the network, and the one test
that signs for real (test_presigned_url_is_signed_for_r2) uses
generate_presigned_url, which is pure local HMAC computation.
"""

from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, override_settings

from services.storage.r2 import (
    DELETE_BATCH_SIZE,
    PRESIGN_EXPIRY_SECONDS,
    R2Service,
)

R2_SETTINGS = dict(
    FILE_STORAGE_PROVIDER="r2",
    R2_ACCOUNT_ID="testaccount",
    R2_ACCESS_KEY_ID="test-access-key",
    R2_SECRET_ACCESS_KEY="test-secret-key",
    R2_BUCKET="goatza-test",
    MEDIA_PUBLIC_BASE_URL="https://media.example.test",
)

USER_ID = "11111111-1111-1111-1111-111111111111"
ORG_ID = "22222222-2222-2222-2222-222222222222"


class _Stub:
    def __init__(self, id):
        self.id = id


class _Actor:
    """Minimal stand-in for core.actor.Actor — no DB needed."""

    def __init__(self, actor_type, user=None, organization=None):
        self.actor_type = actor_type
        self.user = user
        self.organization = organization

    @property
    def is_user(self):
        return self.actor_type == "user"

    @property
    def is_org(self):
        return self.actor_type == "organization"


def user_actor():
    return _Actor("user", user=_Stub(USER_ID))


def org_actor():
    return _Actor("organization", organization=_Stub(ORG_ID))


def image(size_bytes=100_000, content_type="image/webp", kind="image"):
    return {"kind": kind, "content_type": content_type, "size_bytes": size_bytes}


def video(size_bytes=5_000_000, content_type="video/mp4"):
    return {"kind": "video", "content_type": content_type, "size_bytes": size_bytes}


def thumb():
    return {"kind": "thumb", "content_type": "image/webp", "size_bytes": 20_000}


@override_settings(**R2_SETTINGS)
class R2ServiceUploadConfigTests(SimpleTestCase):
    """Key generation, naming semantics, and the response contract."""

    def setUp(self):
        patcher = patch("services.storage.r2.boto3.client")
        self.boto_client = patcher.start()
        self.addCleanup(patcher.stop)

        self.s3 = MagicMock()
        self.s3.generate_presigned_url.side_effect = (
            lambda op, Params, ExpiresIn, HttpMethod:
                f"https://signed.example.test/{Params['Key']}?X-Amz-Expires={ExpiresIn}"
        )
        self.boto_client.return_value = self.s3

        self.service = R2Service()

    def keys_for(self, actor, upload_type, files):
        config = self.service.get_upload_config(
            actor=actor, upload_type=upload_type, files=files
        )
        return config, [u["key"] for u in config["uploads"]]

    # ---------- folder scheme (doc §3.2) ----------

    def test_profile_key_is_the_fixed_user_slot(self):
        _, keys = self.keys_for(user_actor(), "profile", [image()])
        self.assertEqual(keys, [f"users/{USER_ID}/profile.webp"])

    def test_cover_key_is_the_fixed_user_slot(self):
        _, keys = self.keys_for(
            user_actor(), "cover", [image(content_type="image/jpeg")]
        )
        self.assertEqual(keys, [f"users/{USER_ID}/cover.jpg"])

    def test_organization_logo_key_is_the_fixed_org_slot(self):
        _, keys = self.keys_for(
            org_actor(), "organization_logo", [image(content_type="image/png")]
        )
        self.assertEqual(keys, [f"organizations/{ORG_ID}/logo.png"])

    def test_organization_cover_key_is_the_fixed_org_slot(self):
        _, keys = self.keys_for(org_actor(), "organization_cover", [image()])
        self.assertEqual(keys, [f"organizations/{ORG_ID}/cover.webp"])

    def test_user_post_keys_share_one_temp_post_folder(self):
        config, keys = self.keys_for(
            user_actor(), "posts", [image(), thumb(), image(), thumb()]
        )

        temp_post_id = config["temp_post_id"]
        self.assertTrue(temp_post_id)

        prefix = f"users/{USER_ID}/posts/{temp_post_id}/"
        for key in keys:
            self.assertTrue(key.startswith(prefix), key)
            self.assertTrue(key.endswith(".webp"), key)

        # Random UUID names: four entries, four distinct objects.
        self.assertEqual(len(set(keys)), 4)

    def test_org_post_keys_use_the_organization_folder(self):
        config, keys = self.keys_for(org_actor(), "posts", [image()])
        self.assertTrue(
            keys[0].startswith(
                f"organizations/{ORG_ID}/posts/{config['temp_post_id']}/"
            ),
            keys[0],
        )

    def test_recruitment_keys_use_the_org_recruitments_folder(self):
        config, keys = self.keys_for(
            org_actor(), "recruitments", [video(), thumb()]
        )

        prefix = f"organizations/{ORG_ID}/recruitments/{config['temp_post_id']}/"
        self.assertTrue(keys[0].startswith(prefix), keys[0])
        self.assertTrue(keys[0].endswith(".mp4"))
        # The poster lands beside the video, in the same folder (doc §5.1).
        self.assertTrue(keys[1].startswith(prefix), keys[1])
        self.assertTrue(keys[1].endswith(".webp"))

    def test_chat_keys_are_scoped_to_the_sending_user(self):
        _, keys = self.keys_for(user_actor(), "chat", [image(), thumb()])
        for key in keys:
            self.assertTrue(key.startswith(f"chat/users/{USER_ID}/"), key)

    def test_chat_keys_are_scoped_to_the_sending_org(self):
        _, keys = self.keys_for(
            org_actor(), "chat", [video(content_type="video/webm"), thumb()]
        )
        self.assertTrue(keys[0].startswith(f"chat/organizations/{ORG_ID}/"))
        self.assertTrue(keys[0].endswith(".webm"))

    def test_achievements_and_matches_keys_are_user_scoped(self):
        for upload_type, content_type, ext in (
            ("achievements", "image/jpeg", "jpg"),
            ("matches", "image/png", "png"),
        ):
            with self.subTest(upload_type=upload_type):
                _, keys = self.keys_for(
                    user_actor(), upload_type,
                    [image(content_type=content_type)],
                )
                self.assertTrue(
                    keys[0].startswith(f"users/{USER_ID}/{upload_type}/"),
                    keys[0],
                )
                self.assertTrue(keys[0].endswith(f".{ext}"), keys[0])

    def test_chat_config_has_no_temp_post_id(self):
        config, _ = self.keys_for(user_actor(), "chat", [image()])
        self.assertNotIn("temp_post_id", config)

    def test_invalid_upload_type_raises(self):
        with self.assertRaises(ValueError):
            self.service.get_upload_config(
                actor=user_actor(), upload_type="nope", files=[image()]
            )

    # ---------- fixed vs random naming ----------

    def test_fixed_slots_reuse_the_same_key_across_calls(self):
        _, first = self.keys_for(user_actor(), "profile", [image()])
        _, second = self.keys_for(user_actor(), "profile", [image()])
        # Replacing a profile photo must overwrite the object in place.
        self.assertEqual(first, second)

    def test_random_slots_get_a_new_key_every_call(self):
        _, first = self.keys_for(user_actor(), "achievements", [image()])
        _, second = self.keys_for(user_actor(), "achievements", [image()])
        # A new achievement image must never clobber an existing one.
        self.assertNotEqual(first, second)

    def test_each_post_call_gets_a_fresh_temp_post_id(self):
        first, _ = self.keys_for(user_actor(), "posts", [image()])
        second, _ = self.keys_for(user_actor(), "posts", [image()])
        self.assertNotEqual(first["temp_post_id"], second["temp_post_id"])

    # ---------- extension mapping ----------

    def test_extension_is_derived_from_the_content_type(self):
        cases = [
            ("image/webp", "image", "webp"),
            ("image/jpeg", "image", "jpg"),
            ("image/png", "image", "png"),
            ("video/mp4", "video", "mp4"),
            ("video/webm", "video", "webm"),
        ]

        for content_type, kind, ext in cases:
            with self.subTest(content_type=content_type):
                entry = {
                    "kind": kind,
                    "content_type": content_type,
                    "size_bytes": 1234,
                }
                _, keys = self.keys_for(user_actor(), "posts", [entry])
                self.assertTrue(keys[0].endswith(f".{ext}"), keys[0])

    def test_unmapped_content_type_is_refused_before_signing(self):
        # The view's policy check catches this first; R2Service refuses too so
        # an extension-less key can never be signed.
        with self.assertRaises(ValueError):
            self.service.get_upload_config(
                actor=user_actor(),
                upload_type="posts",
                files=[image(content_type="video/quicktime")],
            )

    # ---------- response contract (doc §5.1) ----------

    def test_upload_entry_shape(self):
        config = self.service.get_upload_config(
            actor=user_actor(), upload_type="profile", files=[image()]
        )

        self.assertEqual(config["provider"], "r2")
        self.assertEqual(len(config["uploads"]), 1)

        upload = config["uploads"][0]
        self.assertEqual(
            set(upload),
            {"method", "upload_url", "key", "public_url", "headers", "expires_in"},
        )
        self.assertEqual(upload["method"], "PUT")
        self.assertEqual(upload["headers"], {"Content-Type": "image/webp"})
        self.assertEqual(upload["expires_in"], PRESIGN_EXPIRY_SECONDS)
        self.assertEqual(upload["expires_in"], 600)

    def test_public_url_is_the_delivery_base_plus_key(self):
        config = self.service.get_upload_config(
            actor=user_actor(), upload_type="profile", files=[image()]
        )
        upload = config["uploads"][0]
        self.assertEqual(
            upload["public_url"],
            f"https://media.example.test/{upload['key']}",
        )

    def test_presign_binds_bucket_key_and_content_type(self):
        self.service.get_upload_config(
            actor=user_actor(),
            upload_type="posts",
            files=[video(), thumb()],
        )

        self.assertEqual(self.s3.generate_presigned_url.call_count, 2)

        video_call, thumb_call = self.s3.generate_presigned_url.call_args_list

        for call, content_type in (
            (video_call, "video/mp4"),
            (thumb_call, "image/webp"),
        ):
            self.assertEqual(call.args, ("put_object",))
            self.assertEqual(call.kwargs["Params"]["Bucket"], "goatza-test")
            self.assertEqual(
                call.kwargs["Params"]["ContentType"], content_type
            )
            self.assertTrue(call.kwargs["Params"]["Key"])
            self.assertEqual(call.kwargs["ExpiresIn"], 600)
            self.assertEqual(call.kwargs["HttpMethod"], "PUT")

    def test_uploads_keep_the_requested_file_order(self):
        config = self.service.get_upload_config(
            actor=user_actor(),
            upload_type="posts",
            files=[
                image(content_type="image/png"),
                thumb(),
                image(content_type="image/jpeg"),
            ],
        )
        self.assertEqual(
            [u["headers"]["Content-Type"] for u in config["uploads"]],
            ["image/png", "image/webp", "image/jpeg"],
        )

    # ---------- client construction ----------

    def test_client_is_built_once_and_configured_for_r2(self):
        self.service.get_upload_config(
            actor=user_actor(), upload_type="profile", files=[image()]
        )
        self.service.get_upload_config(
            actor=user_actor(), upload_type="cover", files=[image()]
        )

        self.boto_client.assert_called_once()

        args, kwargs = self.boto_client.call_args
        self.assertEqual(args, ("s3",))
        self.assertEqual(
            kwargs["endpoint_url"],
            "https://testaccount.r2.cloudflarestorage.com",
        )
        self.assertEqual(kwargs["region_name"], "auto")
        self.assertEqual(kwargs["aws_access_key_id"], "test-access-key")
        self.assertEqual(kwargs["aws_secret_access_key"], "test-secret-key")
        self.assertEqual(kwargs["config"].signature_version, "s3v4")

    # ---------- dead capabilities (doc §4 G1/G4) ----------

    def test_get_media_metadata_returns_empty(self):
        self.assertEqual(self.service.get_media_metadata("some/key.mp4", "video"), {})

    def test_ensure_video_derivatives_is_a_noop(self):
        self.assertIsNone(self.service.ensure_video_derivatives("some/key.mp4"))
        self.s3.assert_not_called()


@override_settings(**R2_SETTINGS)
class R2ServiceRealSignatureTests(SimpleTestCase):
    """
    One end-to-end signing pass with the real boto3 client. Presigning is pure
    local HMAC — no request leaves the process — and it is the only way to catch
    a wrong endpoint, region, or signature version before a live round-trip.
    """

    def test_presigned_url_is_signed_for_r2(self):
        config = R2Service().get_upload_config(
            actor=user_actor(), upload_type="profile", files=[image()]
        )
        url = config["uploads"][0]["upload_url"]

        self.assertTrue(
            url.startswith(
                "https://testaccount.r2.cloudflarestorage.com/goatza-test/"
                f"users/{USER_ID}/profile.webp?"
            ),
            url,
        )
        self.assertIn("X-Amz-Algorithm=AWS4-HMAC-SHA256", url)
        self.assertIn("X-Amz-Expires=600", url)
        self.assertIn("X-Amz-Signature=", url)
        # Content-Type has to be part of the signed headers, otherwise the
        # binding in doc §8.1 is decorative.
        self.assertIn("content-type", url.lower())


@override_settings(**R2_SETTINGS)
class R2ServiceDeleteTests(SimpleTestCase):

    def setUp(self):
        patcher = patch("services.storage.r2.boto3.client")
        self.boto_client = patcher.start()
        self.addCleanup(patcher.stop)

        self.s3 = MagicMock()
        self.boto_client.return_value = self.s3

        self.service = R2Service()

    def _paginate_over(self, total_objects, page_size=1000):
        """Make list_objects_v2 yield `total_objects` keys across pages."""
        pages = []
        for start in range(0, total_objects, page_size):
            count = min(page_size, total_objects - start)
            pages.append({
                "Contents": [
                    {"Key": f"users/1/posts/abc/{i}.webp"}
                    for i in range(start, start + count)
                ]
            })

        paginator = MagicMock()
        paginator.paginate.return_value = pages or [{}]
        self.s3.get_paginator.return_value = paginator
        return paginator

    # ---------- delete_file ----------

    def test_delete_file_deletes_the_key(self):
        self.service.delete_file("users/1/profile.webp")
        self.s3.delete_object.assert_called_once_with(
            Bucket="goatza-test", Key="users/1/profile.webp"
        )

    def test_delete_file_ignores_an_empty_key(self):
        self.service.delete_file("")
        self.s3.delete_object.assert_not_called()

    def test_delete_file_never_raises_into_the_caller(self):
        self.s3.delete_object.side_effect = Exception("R2 is down")
        # Deletes run on remove/replace paths; a storage outage must not
        # surface as a failed request.
        self.service.delete_file("users/1/profile.webp")

    # ---------- delete_folder_data ----------

    def test_delete_folder_data_lists_the_prefix_and_deletes_what_it_finds(self):
        paginator = self._paginate_over(3)

        self.service.delete_folder_data("users/1/posts/abc")

        paginator.paginate.assert_called_once_with(
            Bucket="goatza-test", Prefix="users/1/posts/abc"
        )
        self.s3.delete_objects.assert_called_once()
        objects = self.s3.delete_objects.call_args.kwargs["Delete"]["Objects"]
        self.assertEqual(len(objects), 3)
        self.assertEqual(objects[0], {"Key": "users/1/posts/abc/0.webp"})

    def test_delete_folder_data_batches_at_the_delete_objects_limit(self):
        self._paginate_over(2500)

        self.service.delete_folder_data("users/1/posts/abc")

        batch_sizes = [
            len(call.kwargs["Delete"]["Objects"])
            for call in self.s3.delete_objects.call_args_list
        ]
        # 1000 is the S3 DeleteObjects hard limit — a 2500-key prefix has to go
        # out as 1000 + 1000 + 500, not one oversized request.
        self.assertEqual(batch_sizes, [DELETE_BATCH_SIZE, DELETE_BATCH_SIZE, 500])

    def test_delete_folder_data_tolerates_an_empty_prefix(self):
        self._paginate_over(0)

        self.service.delete_folder_data("users/1/posts/gone")

        self.s3.delete_objects.assert_not_called()

    def test_delete_folder_data_ignores_a_blank_prefix(self):
        self.service.delete_folder_data("")
        self.s3.get_paginator.assert_not_called()

    def test_delete_folder_data_never_raises_into_the_caller(self):
        self.s3.get_paginator.side_effect = Exception("R2 is down")
        self.service.delete_folder_data("users/1/posts/abc")
