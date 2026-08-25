"""
Upload-config endpoint (GOATZA_R2_MIGRATION.md §5.1 + the §7 policy table).

boto3 is mocked — no network. The POST contract is the live one; the GET tests
cover the legacy Cloudinary contract that stays reachable only while
FILE_STORAGE_PROVIDER is flipped back as a rollback.
"""

from unittest.mock import MagicMock, patch

from django.core.cache import cache
from django.test import override_settings
from rest_framework.test import APITestCase

from accounts.models import User
from organization.models import (
    Organization,
    OrganizationProfile,
    OrganizationMember,
)

URL = "/user/get/upload/signature"

MB = 1024 * 1024

R2_SETTINGS = dict(
    FILE_STORAGE_PROVIDER="r2",
    R2_ACCOUNT_ID="testaccount",
    R2_ACCESS_KEY_ID="test-access-key",
    R2_SECRET_ACCESS_KEY="test-secret-key",
    R2_BUCKET="goatza-test",
    MEDIA_PUBLIC_BASE_URL="https://media.example.test",
)

CLOUDINARY_SETTINGS = dict(
    FILE_STORAGE_PROVIDER="cloudinary",
    CLOUDINARY_CLOUD_NAME="test-cloud",
    CLOUDINARY_API_KEY="test-key",
    CLOUDINARY_API_SECRET="test-secret",
)


def image(size_bytes=100_000, content_type="image/webp"):
    return {"kind": "image", "content_type": content_type, "size_bytes": size_bytes}


def video(size_bytes=5 * MB, content_type="video/mp4"):
    return {"kind": "video", "content_type": content_type, "size_bytes": size_bytes}


def thumb(size_bytes=20_000, content_type="image/webp"):
    return {"kind": "thumb", "content_type": content_type, "size_bytes": size_bytes}


class UploadConfigTestCase(APITestCase):
    """Shared fixtures: one user, one org they belong to, one they don't."""

    def setUp(self):
        # DRF's UserRateThrottle counts through the default cache, which is
        # shared across tests in the process — a class this size would trip the
        # 100/min user rate without this.
        cache.clear()

        # boto3 is never real in these tests. generate_presigned_url must hand
        # back a real string: DRF's JSON encoder probes an unknown object for
        # `tolist`/`__iter__`, which a bare MagicMock answers forever.
        patcher = patch("services.storage.r2.boto3.client")
        self.boto_client = patcher.start()
        self.addCleanup(patcher.stop)

        self.s3 = MagicMock()
        self.s3.generate_presigned_url.side_effect = (
            lambda op, Params, ExpiresIn, HttpMethod:
                f"https://signed.example.test/{Params['Key']}"
        )
        self.boto_client.return_value = self.s3

        self.user = User.objects.create_user(
            email="player@example.com", password="pass1234"
        )
        self.org = self._org("Dream FC", "dreamfc")
        OrganizationMember.objects.create(
            organization=self.org,
            user=self.user,
            role=OrganizationMember.Role.OWNER,
        )
        self.foreign_org = self._org("Rival Club", "rivalfc")

        self.client.force_authenticate(user=self.user)

    def _org(self, name, username):
        org = Organization.objects.create(
            name=name,
            username=username,
            type=Organization.Type.CLUB,
        )
        OrganizationProfile.objects.create(organization=org)
        return org

    # ── request helpers ──────────────────────────────────────────

    def post(self, upload_type, files, *, org=None, org_id=None):
        body = {"type": upload_type, "files": files}
        if org_id is not None:
            body["org_id"] = str(org_id)

        headers = {}
        if org is not None:
            headers = {
                "HTTP_X_ACTOR_TYPE": "organization",
                "HTTP_X_ACTOR_ID": str(org.id),
            }

        return self.client.post(URL, body, format="json", **headers)

    def keys(self, res):
        return [u["key"] for u in res.data["data"]["uploads"]]


@override_settings(**R2_SETTINGS)
class UploadConfigPostHappyPathTests(UploadConfigTestCase):
    """One case per upload_type, checking the key the client will get back."""

    def test_profile(self):
        res = self.post("profile", [image()])

        self.assertEqual(res.status_code, 200, res.data)
        self.assertTrue(res.data["success"])
        self.assertEqual(res.data["data"]["provider"], "r2")
        self.assertEqual(self.keys(res), [f"users/{self.user.id}/profile.webp"])

    def test_profile_upload_entry_is_the_documented_shape(self):
        res = self.post("profile", [image(content_type="image/jpeg")])

        upload = res.data["data"]["uploads"][0]
        self.assertEqual(upload["method"], "PUT")
        self.assertEqual(upload["headers"], {"Content-Type": "image/jpeg"})
        self.assertEqual(upload["expires_in"], 600)
        self.assertEqual(
            upload["public_url"],
            f"https://media.example.test/users/{self.user.id}/profile.jpg",
        )

    def test_cover(self):
        res = self.post("cover", [image()])
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(self.keys(res), [f"users/{self.user.id}/cover.webp"])

    def test_achievements(self):
        res = self.post("achievements", [image()])
        self.assertEqual(res.status_code, 200, res.data)
        self.assertTrue(
            self.keys(res)[0].startswith(f"users/{self.user.id}/achievements/")
        )

    def test_matches(self):
        res = self.post("matches", [image()])
        self.assertEqual(res.status_code, 200, res.data)
        self.assertTrue(
            self.keys(res)[0].startswith(f"users/{self.user.id}/matches/")
        )

    def test_posts_images_with_thumbs_as_user(self):
        res = self.post("posts", [image(), thumb(), image(), thumb()])

        self.assertEqual(res.status_code, 200, res.data)
        temp = res.data["data"]["temp_post_id"]
        self.assertTrue(temp)

        for key in self.keys(res):
            self.assertTrue(
                key.startswith(f"users/{self.user.id}/posts/{temp}/"), key
            )

    def test_posts_ten_images_with_thumbs_is_the_ceiling(self):
        files = []
        for _ in range(10):
            files += [image(), thumb()]

        res = self.post("posts", files)

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(len(res.data["data"]["uploads"]), 20)

    def test_posts_video_with_poster(self):
        res = self.post("posts", [video(), thumb()])

        self.assertEqual(res.status_code, 200, res.data)
        keys = self.keys(res)
        self.assertTrue(keys[0].endswith(".mp4"))
        self.assertTrue(keys[1].endswith(".webp"))
        # Poster and clip land in the same temp-post folder.
        self.assertEqual(keys[0].rsplit("/", 1)[0], keys[1].rsplit("/", 1)[0])

    def test_posts_as_org_actor(self):
        res = self.post("posts", [image()], org=self.org)

        self.assertEqual(res.status_code, 200, res.data)
        self.assertTrue(
            self.keys(res)[0].startswith(f"organizations/{self.org.id}/posts/")
        )

    def test_chat_as_user(self):
        res = self.post("chat", [image(), thumb()])

        self.assertEqual(res.status_code, 200, res.data)
        for key in self.keys(res):
            self.assertTrue(key.startswith(f"chat/users/{self.user.id}/"), key)

    def test_chat_as_org(self):
        res = self.post("chat", [video(), thumb()], org=self.org)

        self.assertEqual(res.status_code, 200, res.data)
        self.assertTrue(
            self.keys(res)[0].startswith(f"chat/organizations/{self.org.id}/")
        )

    def test_organization_logo(self):
        res = self.post("organization_logo", [image()], org=self.org)

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(
            self.keys(res), [f"organizations/{self.org.id}/logo.webp"]
        )

    def test_organization_cover(self):
        res = self.post(
            "organization_cover", [image(content_type="image/png")], org=self.org
        )

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(
            self.keys(res), [f"organizations/{self.org.id}/cover.png"]
        )

    def test_recruitments(self):
        res = self.post("recruitments", [video(), thumb()], org=self.org)

        self.assertEqual(res.status_code, 200, res.data)
        temp = res.data["data"]["temp_post_id"]
        for key in self.keys(res):
            self.assertTrue(
                key.startswith(
                    f"organizations/{self.org.id}/recruitments/{temp}/"
                ),
                key,
            )


@override_settings(**R2_SETTINGS)
class UploadConfigPolicyRejectionTests(UploadConfigTestCase):
    """Doc §7 — every rejection returns 400 and never signs anything."""

    def assertRejected(self, res, contains=None):
        self.assertEqual(res.status_code, 400, res.data)
        self.assertFalse(res.data["success"])
        if contains:
            self.assertIn(contains, res.data["message"])
        # Nothing may reach the signer on a rejected request.
        self.boto_client.assert_not_called()

    # ---------- shape ----------

    def test_missing_files(self):
        res = self.client.post(URL, {"type": "profile"}, format="json")
        self.assertRejected(res, "At least one file is required")

    def test_empty_files(self):
        self.assertRejected(
            self.post("profile", []), "At least one file is required"
        )

    def test_files_not_a_list(self):
        self.assertRejected(
            self.post("profile", {"kind": "image"}),
            "At least one file is required",
        )

    def test_file_entry_not_an_object(self):
        self.assertRejected(self.post("profile", ["image.webp"]), "Invalid file entry")

    def test_invalid_upload_type(self):
        self.assertRejected(self.post("banner", [image()]), "Invalid upload type")

    # ---------- content type ----------

    def test_disallowed_content_type(self):
        self.assertRejected(
            self.post("posts", [video(content_type="video/quicktime"), thumb()]),
            "Unsupported file type: video/quicktime",
        )

    def test_gif_is_not_an_allowed_image(self):
        self.assertRejected(
            self.post("profile", [image(content_type="image/gif")]),
            "Unsupported file type: image/gif",
        )

    def test_video_content_type_on_an_image_only_type(self):
        self.assertRejected(
            self.post("achievements", [video()]),
            "Unsupported upload kind for this type: video",
        )

    def test_thumb_kind_on_an_image_only_type(self):
        self.assertRejected(
            self.post("profile", [thumb()]),
            "Unsupported upload kind for this type: thumb",
        )

    def test_unknown_kind(self):
        self.assertRejected(
            self.post(
                "posts",
                [{"kind": "audio", "content_type": "image/webp", "size_bytes": 10}],
            ),
            "Unsupported upload kind for this type: audio",
        )

    # ---------- size ----------

    def test_oversize_image(self):
        self.assertRejected(
            self.post("profile", [image(size_bytes=5 * MB + 1)]),
            "File too large",
        )

    def test_image_exactly_at_the_cap_is_allowed(self):
        res = self.post("profile", [image(size_bytes=5 * MB)])
        self.assertEqual(res.status_code, 200, res.data)

    def test_oversize_video(self):
        self.assertRejected(
            self.post("posts", [video(size_bytes=80 * MB + 1), thumb()]),
            "File too large",
        )

    def test_oversize_thumb(self):
        self.assertRejected(
            self.post("posts", [video(), thumb(size_bytes=1 * MB + 1)]),
            "File too large",
        )

    def test_zero_size(self):
        self.assertRejected(
            self.post("profile", [image(size_bytes=0)]), "Invalid file size"
        )

    def test_negative_size(self):
        self.assertRejected(
            self.post("profile", [image(size_bytes=-1)]), "Invalid file size"
        )

    def test_non_integer_size(self):
        self.assertRejected(
            self.post("profile", [image(size_bytes="100000")]), "Invalid file size"
        )

    def test_boolean_size(self):
        # bool is an int subclass — True must not pass as a 1-byte file.
        self.assertRejected(
            self.post("profile", [image(size_bytes=True)]), "Invalid file size"
        )

    # ---------- counts ----------

    def test_too_many_files_for_a_fixed_slot(self):
        self.assertRejected(
            self.post("profile", [image(), image()]), "Too many files"
        )

    def test_too_many_files_for_posts(self):
        files = [image() for _ in range(21)]
        self.assertRejected(self.post("posts", files), "Too many files")

    def test_too_many_images_for_one_post(self):
        # Under max_files (20) but over the 10-image ceiling.
        files = [image() for _ in range(11)]
        self.assertRejected(self.post("posts", files), "Too many files")

    def test_too_many_files_for_a_chat_message(self):
        self.assertRejected(
            self.post("chat", [image(), thumb(), image()]), "Too many files"
        )

    # ---------- video / thumb pairing ----------

    def test_video_without_thumb(self):
        self.assertRejected(
            self.post("posts", [video()]),
            "A video upload must be requested on its own",
        )

    def test_video_with_two_thumbs(self):
        self.assertRejected(
            self.post("posts", [video(), thumb(), thumb()]),
            "A video upload must be requested on its own",
        )

    def test_two_videos_in_one_request(self):
        self.assertRejected(
            self.post("posts", [video(), video(), thumb(), thumb()]),
            "A video upload must be requested on its own",
        )

    def test_video_mixed_with_images(self):
        self.assertRejected(
            self.post("posts", [video(), thumb(), image()]),
            "A video upload must be requested on its own",
        )

    def test_thumb_without_a_primary(self):
        self.assertRejected(
            self.post("posts", [thumb()]),
            "A thumbnail must accompany the image or video",
        )

    def test_more_thumbs_than_images(self):
        self.assertRejected(
            self.post("posts", [image(), thumb(), thumb()]),
            "A thumbnail must accompany the image or video",
        )


@override_settings(**R2_SETTINGS)
class UploadConfigGuardTests(UploadConfigTestCase):
    """Actor-type and org-membership guards — messages unchanged from the GET era."""

    # ---------- actor type ----------

    def test_user_only_type_as_org_actor(self):
        for upload_type in ("profile", "cover", "achievements", "matches"):
            with self.subTest(upload_type=upload_type):
                res = self.post(upload_type, [image()], org=self.org)
                self.assertEqual(res.status_code, 403, res.data)
                self.assertEqual(
                    res.data["message"],
                    "Switch to your personal account for this upload",
                )

    def test_org_only_type_as_user_actor(self):
        for upload_type, files in (
            ("organization_logo", [image()]),
            ("organization_cover", [image()]),
            ("recruitments", [image()]),
        ):
            with self.subTest(upload_type=upload_type):
                res = self.post(upload_type, files)
                self.assertEqual(res.status_code, 403, res.data)
                self.assertEqual(
                    res.data["message"],
                    "Switch to your organization account for this upload",
                )

    # ---------- org_id in the body ----------

    def test_org_id_switches_the_actor_to_that_org(self):
        res = self.post("organization_logo", [image()], org_id=self.org.id)

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(
            self.keys(res), [f"organizations/{self.org.id}/logo.webp"]
        )

    def test_org_id_of_an_org_the_user_does_not_belong_to(self):
        res = self.post(
            "organization_logo", [image()], org_id=self.foreign_org.id
        )

        self.assertEqual(res.status_code, 400, res.data)
        self.assertEqual(
            res.data["message"], "You are not a member of this organization"
        )

    def test_unknown_org_id(self):
        res = self.post(
            "organization_logo",
            [image()],
            org_id="33333333-3333-3333-3333-333333333333",
        )

        self.assertEqual(res.status_code, 404, res.data)
        self.assertEqual(res.data["message"], "Organization not found")

    # ---------- auth ----------

    def test_unauthenticated_is_rejected(self):
        self.client.force_authenticate(user=None)
        res = self.client.post(
            URL, {"type": "profile", "files": [image()]}, format="json"
        )
        self.assertEqual(res.status_code, 401)


class UploadConfigProviderTests(UploadConfigTestCase):
    """The v1/v2 contracts each belong to exactly one provider."""

    @override_settings(**CLOUDINARY_SETTINGS)
    def test_post_is_refused_on_the_cloudinary_provider(self):
        res = self.post("profile", [image()])

        self.assertEqual(res.status_code, 400, res.data)
        self.assertEqual(
            res.data["message"], "upload config v2 requires the r2 provider"
        )

    @override_settings(**R2_SETTINGS)
    def test_get_is_refused_on_the_r2_provider(self):
        # R2Service signs a declared `files` list; there is no way to serve the
        # old count-based contract from it.
        res = self.client.get(URL, {"type": "profile", "count": 1})

        self.assertEqual(res.status_code, 400, res.data)
        self.assertEqual(
            res.data["message"],
            "upload config v1 requires the cloudinary provider",
        )

    # ---------- legacy GET, unchanged behaviour ----------

    @override_settings(**CLOUDINARY_SETTINGS)
    def test_get_still_serves_the_cloudinary_contract(self):
        res = self.client.get(URL, {"type": "profile", "count": 1})

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["data"]["provider"], "cloudinary")

        upload = res.data["data"]["uploads"][0]
        self.assertEqual(upload["folder"], f"users/{self.user.id}/profile")
        self.assertEqual(upload["public_id"], "profile")
        self.assertEqual(upload["overwrite"], "true")
        self.assertTrue(upload["signature"])

    @override_settings(**CLOUDINARY_SETTINGS)
    def test_get_rejects_an_invalid_count(self):
        for count in (0, 11):
            with self.subTest(count=count):
                res = self.client.get(URL, {"type": "profile", "count": count})
                self.assertEqual(res.status_code, 400, res.data)
                self.assertEqual(
                    res.data["message"], "Invalid count (1-10 allowed)"
                )

    @override_settings(**CLOUDINARY_SETTINGS)
    def test_get_rejects_an_invalid_type(self):
        res = self.client.get(URL, {"type": "banner", "count": 1})

        self.assertEqual(res.status_code, 400, res.data)
        self.assertEqual(res.data["message"], "Invalid upload type")

    @override_settings(**CLOUDINARY_SETTINGS)
    def test_get_keeps_its_actor_type_guard(self):
        res = self.client.get(
            URL,
            {"type": "profile", "count": 1},
            HTTP_X_ACTOR_TYPE="organization",
            HTTP_X_ACTOR_ID=str(self.org.id),
        )

        self.assertEqual(res.status_code, 403, res.data)
        self.assertEqual(
            res.data["message"],
            "Switch to your personal account for this upload",
        )

    @override_settings(**CLOUDINARY_SETTINGS)
    def test_get_keeps_its_org_membership_guard(self):
        res = self.client.get(
            URL,
            {
                "type": "organization_logo",
                "count": 1,
                "org_id": str(self.foreign_org.id),
            },
        )

        self.assertEqual(res.status_code, 400, res.data)
        self.assertEqual(
            res.data["message"], "You are not a member of this organization"
        )
