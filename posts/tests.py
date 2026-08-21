from unittest.mock import patch

from django.test import override_settings
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import User, UserProfile
from connections.models import Follow
from notifications.models import Notification
from organization.models import (
    Organization,
    OrganizationProfile,
    OrganizationMember,
)
from posts.models import (
    Post, PostMedia, Like, Comment, Hashtag, PostHashtag, PostMention, SavedPost,
)
from sports.models import Sport

SEARCH_URL = "/posts/search"
LIST_URL = "/posts/list"
CREATE_URL = "/posts/create"
UPDATE_URL = "/posts/update"
COMMENT_DELETE_URL = "/posts/comments/delete"
MY_MENTIONS_URL = "/posts/mentions/my"
MENTION_SUGGEST_URL = "/posts/mention/suggest"
SAVE_URL = "/posts/save"
SAVED_LIST_URL = "/posts/saved/list"


class PostSearchTests(APITestCase):
    """GET /posts/search — content + hashtag search over public, live posts."""

    def setUp(self):
        self.me = self._user("me", "Me")

    # ── factories ────────────────────────────────────────────────

    def _user(self, username, name):
        user = User.objects.create_user(
            email=f"{username}@example.com",
            password="pass1234",
            username=username,
        )
        UserProfile.objects.create(
            user=user, name=name,
            profile_photo=f"https://cdn.example.com/{username}.jpg",
        )
        return user

    def _org(self, username, name):
        org = Organization.objects.create(
            name=name, username=username, type=Organization.Type.CLUB
        )
        OrganizationProfile.objects.create(
            organization=org, logo=f"https://cdn.example.com/{username}.png"
        )
        return org

    def _post(self, author, content="", visibility=Post.Visibility.PUBLIC,
              is_deleted=False, hashtags=None):
        kwargs = dict(
            content=content, visibility=visibility, is_deleted=is_deleted,
        )
        if isinstance(author, User):
            kwargs["author_user"] = author
        else:
            kwargs["author_org"] = author
        post = Post.objects.create(**kwargs)

        for tag in (hashtags or []):
            hashtag, _ = Hashtag.objects.get_or_create(name=tag)
            PostHashtag.objects.create(post=post, hashtag=hashtag)
        return post

    # ── request helpers ──────────────────────────────────────────

    def _get(self, actor_user=None, org=None, **params):
        self.client.force_authenticate(user=actor_user or self.me)
        headers = {}
        if org is not None:
            headers = {
                "HTTP_X_ACTOR_TYPE": "organization",
                "HTTP_X_ACTOR_ID": str(org.id),
            }
        return self.client.get(SEARCH_URL, params, **headers)

    def _ids(self, resp):
        return [str(r["id"]) for r in resp.data["data"]["results"]]

    # ── content matching ─────────────────────────────────────────

    def test_content_match_case_insensitive(self):
        stranger = self._user("s", "S")
        hit = self._post(stranger, content="Amazing Football trial today")
        miss = self._post(stranger, content="Cricket nets session")

        ids = self._ids(self._get(q="FOOTBALL"))
        self.assertIn(str(hit.id), ids)
        self.assertNotIn(str(miss.id), ids)

    # ── hashtag matching ─────────────────────────────────────────

    def test_hashtag_match(self):
        stranger = self._user("s", "S")
        hit = self._post(stranger, content="no keyword here", hashtags=["football"])
        miss = self._post(stranger, content="no keyword here", hashtags=["cricket"])

        ids = self._ids(self._get(q="football"))
        self.assertIn(str(hit.id), ids)
        self.assertNotIn(str(miss.id), ids)

    def test_hashtag_match_with_leading_hash_in_query(self):
        stranger = self._user("s", "S")
        hit = self._post(stranger, content="plain text", hashtags=["football"])

        # The user types "#football"; the "#" is stripped before matching.
        ids = self._ids(self._get(q="#football"))
        self.assertIn(str(hit.id), ids)

    def test_content_and_two_hashtags_appears_once(self):
        stranger = self._user("s", "S")
        # Matches on content AND on two separate hashtags — Exists must keep it
        # to a single row (no join duplication, no distinct()).
        post = self._post(
            stranger,
            content="football is life",
            hashtags=["football", "footballer"],
        )

        ids = self._ids(self._get(q="football"))
        self.assertEqual(ids.count(str(post.id)), 1)

    # ── visibility / scope ───────────────────────────────────────

    def test_deleted_and_non_public_excluded_own_public_included(self):
        stranger = self._user("s", "S")
        public = self._post(stranger, content="football public")
        deleted = self._post(stranger, content="football deleted", is_deleted=True)
        followers_only = self._post(
            stranger, content="football followers",
            visibility=Post.Visibility.FOLLOWERS,
        )
        # Search is global — the actor's OWN public post IS included.
        mine = self._post(self.me, content="football mine")

        ids = self._ids(self._get(q="football"))
        self.assertIn(str(public.id), ids)
        self.assertIn(str(mine.id), ids)               # own post included
        self.assertNotIn(str(deleted.id), ids)         # soft-deleted
        self.assertNotIn(str(followers_only.id), ids)  # not public

    def test_org_actor_can_search(self):
        org = self._org("dreamfc", "Dream FC")
        OrganizationMember.objects.create(
            organization=org, user=self.me, role=OrganizationMember.Role.OWNER
        )
        stranger = self._user("s", "S")
        hit = self._post(stranger, content="football highlights")

        ids = self._ids(self._get(org=org, q="football"))
        self.assertIn(str(hit.id), ids)

    # ── query validation ─────────────────────────────────────────

    def test_missing_q_returns_400(self):
        resp = self._get()
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_empty_q_after_trim_returns_400(self):
        resp = self._get(q="   ")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    # ── ordering + reactions ─────────────────────────────────────

    def test_newest_first_ordering(self):
        stranger = self._user("s", "S")
        first = self._post(stranger, content="football one")
        second = self._post(stranger, content="football two")
        third = self._post(stranger, content="football three")

        # UUIDv7 PKs are time-sortable, so -id is newest-first.
        ids = self._ids(self._get(q="football"))
        self.assertEqual(ids, [str(third.id), str(second.id), str(first.id)])

    def test_reaction_context(self):
        stranger = self._user("s", "S")
        post = self._post(stranger, content="football clip")
        Like.objects.create(user=self.me, post=post, type=Like.Type.FIRE)

        resp = self._get(q="football")
        row = next(r for r in resp.data["data"]["results"]
                   if str(r["id"]) == str(post.id))
        self.assertTrue(row["reaction"]["is_reacted"])
        self.assertEqual(row["reaction"]["type"], "fire")

    # ── pagination ───────────────────────────────────────────────

    def test_pagination_continues_via_cursor_no_dupes_or_skips(self):
        stranger = self._user("s", "S")
        created = [
            self._post(stranger, content=f"football post {i}")
            for i in range(20)
        ]

        seen, cursor, pages = [], None, 0
        short_page_cursor = "sentinel"
        while True:
            params = {"q": "football"}
            if cursor:
                params["cursor"] = cursor
            resp = self._get(**params)
            self.assertEqual(resp.status_code, status.HTTP_200_OK)
            page_ids = self._ids(resp)
            self.assertLessEqual(len(page_ids), 15)  # page size
            seen.extend(page_ids)
            cursor = resp.data["data"]["next_cursor"]
            short_page_cursor = cursor
            pages += 1
            if not cursor or pages > 6:
                break

        # 20 posts, each exactly once, none skipped.
        self.assertEqual(len(seen), 20)
        self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual(set(seen), {str(p.id) for p in created})
        # Last page was short (20 = 15 + 5) → next_cursor is null.
        self.assertIsNone(short_page_cursor)

    def test_short_page_has_null_cursor(self):
        stranger = self._user("s", "S")
        for i in range(3):
            self._post(stranger, content=f"football {i}")

        resp = self._get(q="football")
        self.assertIsNone(resp.data["data"]["next_cursor"])

    def test_invalid_cursor_returns_400(self):
        resp = self._get(q="football", cursor="not-a-real-cursor")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)


# =====================================================================
# Media dimensions — server-side extraction + API exposure + fallback
# =====================================================================

CLOUD = "democloud"


@override_settings(
    CLOUDINARY_CLOUD_NAME=CLOUD,
    CLOUDINARY_API_KEY="key",
    CLOUDINARY_API_SECRET="secret",
)
class PostMediaDimensionsTests(APITestCase):
    """
    width/height are extracted server-side on upload (never trusted from the
    client), persisted on PostMedia, returned in list responses, and degrade to
    NULL when extraction fails — so legacy/failed rows still render.
    """

    def setUp(self):
        self.me = self._user("me", "Me")
        self.client.force_authenticate(user=self.me)

    # ── factories ────────────────────────────────────────────────

    def _user(self, username, name):
        user = User.objects.create_user(
            email=f"{username}@example.com",
            password="pass1234",
            username=username,
        )
        UserProfile.objects.create(user=user, name=name)
        return user

    def _image_payload(self, order=0):
        """A create-request media item whose URL/public_id pass validate_media."""
        public_id = f"users/{self.me.id}/posts/temp/pic{order}"
        file_url = (
            f"https://res.cloudinary.com/{CLOUD}/image/upload/v1/{public_id}.jpg"
        )
        return {
            "file_url": file_url,
            "public_id": public_id,
            "media_type": "image",
            "order": order,
        }

    def _video_payload(self, order=0, duration=10):
        public_id = f"users/{self.me.id}/posts/temp/clip{order}"
        file_url = (
            f"https://res.cloudinary.com/{CLOUD}/video/upload/v1/{public_id}.mp4"
        )
        return {
            "file_url": file_url,
            "public_id": public_id,
            "media_type": "video",
            "thumbnail_url": (
                f"https://res.cloudinary.com/{CLOUD}/video/upload/so_0/{public_id}.jpg"
            ),
            "duration": duration,
            "order": order,
        }

    def _list_media(self, post_id):
        resp = self.client.get(LIST_URL, {"post_id": str(post_id)})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = resp.data["data"]["results"]
        self.assertEqual(len(results), 1)
        return results[0]["media"]

    # ── create flow: extraction persists trusted dimensions ──────

    @patch("services.storage.cloudinary.CloudinaryService.get_media_metadata")
    def test_create_persists_server_extracted_dimensions(self, mock_meta):
        mock_meta.return_value = {"width": 1920, "height": 1080}

        resp = self.client.post(
            CREATE_URL,
            {"content": "nice shot", "media": [self._image_payload()]},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)

        media = PostMedia.objects.get()
        self.assertEqual(media.width, 1920)
        self.assertEqual(media.height, 1080)
        # Extraction was driven by the stored public_id, not client dimensions.
        mock_meta.assert_called_once_with(media.public_id, "image")

    # Creating a video also fires the eager-derivative request inline; stubbed
    # so the test never leaves the process.
    @patch("services.storage.cloudinary.CloudinaryService.ensure_video_derivatives")
    @patch("services.storage.cloudinary.CloudinaryService.get_media_metadata")
    def test_server_duration_overrides_client_for_video(self, mock_meta, _mock_eager):
        # Client claims 10s; Cloudinary reports 45s → server value wins.
        mock_meta.return_value = {"width": 1280, "height": 720, "duration": 45}

        resp = self.client.post(
            CREATE_URL,
            {"content": "clip", "media": [self._video_payload(duration=10)]},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)

        media = PostMedia.objects.get()
        self.assertEqual(media.width, 1280)
        self.assertEqual(media.height, 720)
        self.assertEqual(media.duration, 45)

    @patch("services.storage.cloudinary.CloudinaryService.get_media_metadata")
    def test_extraction_failure_stores_null_but_creates_post(self, mock_meta):
        # Cloudinary hiccup → empty dict → NULL dims, upload NOT blocked.
        mock_meta.return_value = {}

        resp = self.client.post(
            CREATE_URL,
            {"content": "still works", "media": [self._image_payload()]},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)

        media = PostMedia.objects.get()
        self.assertIsNone(media.width)
        self.assertIsNone(media.height)

    # ── API exposure ─────────────────────────────────────────────

    def test_list_response_exposes_width_and_height(self):
        post = Post.objects.create(author_user=self.me, content="hi")
        PostMedia.objects.create(
            post=post,
            file_url="https://cdn.example.com/x.jpg",
            public_id="users/x/posts/temp/x",
            media_type=PostMedia.MediaType.IMAGE,
            width=1080,
            height=1350,
            order=0,
        )

        media = self._list_media(post.id)
        self.assertEqual(media[0]["width"], 1080)
        self.assertEqual(media[0]["height"], 1350)

    def test_legacy_media_without_dimensions_serializes_null(self):
        post = Post.objects.create(author_user=self.me, content="old")
        PostMedia.objects.create(
            post=post,
            file_url="https://cdn.example.com/old.jpg",
            public_id="users/x/posts/temp/old",
            media_type=PostMedia.MediaType.IMAGE,
            order=0,
        )

        media = self._list_media(post.id)
        self.assertIsNone(media[0]["width"])
        self.assertIsNone(media[0]["height"])

    # ── storage helper degrades gracefully ───────────────────────

    def test_get_media_metadata_returns_empty_on_provider_error(self):
        from services.storage.cloudinary import CloudinaryService

        with patch("cloudinary.api.resource", side_effect=Exception("boom")):
            meta = CloudinaryService().get_media_metadata("users/x/pic", "image")

        self.assertEqual(meta, {})


# =====================================================================
# Eager video derivatives — the first viewer must not wait on a transcode
# =====================================================================

@override_settings(
    CLOUDINARY_CLOUD_NAME=CLOUD,
    CLOUDINARY_API_KEY="key",
    CLOUDINARY_API_SECRET="secret",
)
class VideoEagerDerivativeTests(APITestCase):
    """
    Cloudinary builds a video derivative on FIRST REQUEST, so without eager
    generation the first person to open a clip sits through a live transcode.
    Creating a video asks for it up front; creating images never does; and a
    provider failure stays invisible to the user.
    """

    def setUp(self):
        self.me = User.objects.create_user(
            email="me@example.com", password="pass1234", username="me",
        )
        UserProfile.objects.create(user=self.me, name="Me")
        self.client.force_authenticate(user=self.me)

    # ── factories (same shape validate_media accepts) ────────────

    def _image_payload(self, order=0):
        public_id = f"users/{self.me.id}/posts/temp/pic{order}"
        return {
            "file_url": (
                f"https://res.cloudinary.com/{CLOUD}/image/upload/v1/{public_id}.jpg"
            ),
            "public_id": public_id,
            "media_type": "image",
            "order": order,
        }

    def _video_payload(self, order=0, duration=10):
        public_id = f"users/{self.me.id}/posts/temp/clip{order}"
        return {
            "file_url": (
                f"https://res.cloudinary.com/{CLOUD}/video/upload/v1/{public_id}.mp4"
            ),
            "public_id": public_id,
            "media_type": "video",
            "duration": duration,
            "order": order,
        }

    # ── create paths ─────────────────────────────────────────────

    @patch("services.storage.cloudinary.CloudinaryService.get_media_metadata",
           return_value={})
    @patch("services.storage.cloudinary.CloudinaryService.ensure_video_derivatives")
    def test_video_post_requests_the_derivative(self, mock_eager, _mock_meta):
        payload = self._video_payload()

        resp = self.client.post(
            CREATE_URL,
            {"content": "clip", "media": [payload]},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)

        media = PostMedia.objects.get()
        mock_eager.assert_called_once_with(media.public_id)

    @patch("services.storage.cloudinary.CloudinaryService.get_media_metadata",
           return_value={})
    @patch("services.storage.cloudinary.CloudinaryService.ensure_video_derivatives")
    def test_image_post_never_requests_a_derivative(self, mock_eager, _mock_meta):
        resp = self.client.post(
            CREATE_URL,
            {"content": "photos", "media": [
                self._image_payload(0), self._image_payload(1),
            ]},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)

        mock_eager.assert_not_called()

    @patch("services.storage.cloudinary.CloudinaryService.get_media_metadata",
           return_value={})
    def test_provider_failure_never_blocks_the_post(self, _mock_meta):
        # The real method runs; only Cloudinary's own call blows up.
        with patch("cloudinary.uploader.explicit", side_effect=Exception("boom")):
            resp = self.client.post(
                CREATE_URL,
                {"content": "still works", "media": [self._video_payload()]},
                format="json",
            )

        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertEqual(Post.objects.count(), 1)
        self.assertEqual(PostMedia.objects.count(), 1)

    # ── the provider call itself ─────────────────────────────────

    def test_explicit_is_called_with_the_canonical_transformation(self):
        """
        The default is HLS OFF: exactly ONE eager entry, the mp4. Its
        transformation has to match VIDEO_DELIVERY_TRANSFORM in the frontend
        byte-for-byte or the pre-generated asset is not the one being requested.
        """
        from services.storage.cloudinary import (
            VIDEO_EAGER_FORMAT,
            VIDEO_EAGER_TRANSFORMATION,
            VIDEO_HLS_FORMAT,
            VIDEO_HLS_TRANSFORMATION,
            CloudinaryService,
        )

        self.assertEqual(
            VIDEO_EAGER_TRANSFORMATION,
            "c_limit,h_1280,w_1280,q_auto:good,vc_h264",
        )
        # Constants stay defined even while the flag is off.
        self.assertEqual(VIDEO_HLS_TRANSFORMATION, "sp_hd")
        self.assertEqual(VIDEO_HLS_FORMAT, "m3u8")

        with patch("cloudinary.uploader.explicit") as mock_explicit:
            CloudinaryService().ensure_video_derivatives("users/x/posts/clip")

        mock_explicit.assert_called_once_with(
            "users/x/posts/clip",
            type="upload",
            resource_type="video",
            eager_async=True,
            eager=[
                {
                    "raw_transformation": VIDEO_EAGER_TRANSFORMATION,
                    "format": VIDEO_EAGER_FORMAT,
                },
            ],
        )

    def test_hls_is_not_requested_while_the_flag_is_off(self):
        """
        The credit-saving default. Nothing in the payload may mention the
        streaming profile — an accidental sp_hd here is billed transformation
        credits nobody asked for.
        """
        from services.storage.cloudinary import CloudinaryService

        with patch("cloudinary.uploader.explicit") as mock_explicit:
            CloudinaryService().ensure_video_derivatives("users/x/posts/clip")

        eager = mock_explicit.call_args.kwargs["eager"]
        self.assertEqual(len(eager), 1)
        self.assertEqual(eager[0]["format"], "mp4")
        self.assertNotIn("sp_hd", str(eager))
        self.assertNotIn("m3u8", str(eager))

    @override_settings(CLOUDINARY_ENABLE_HLS=True)
    def test_hls_entry_is_appended_when_the_flag_is_on(self):
        """
        Flag on → both entries, mp4 FIRST. mp4 is the universal fallback (old
        clips, HLS failures, no-MSE clients), so a future edit that reorders or
        drops it should fail here with an obvious reason.
        """
        from services.storage.cloudinary import (
            VIDEO_EAGER_FORMAT,
            VIDEO_EAGER_TRANSFORMATION,
            VIDEO_HLS_FORMAT,
            VIDEO_HLS_TRANSFORMATION,
            CloudinaryService,
        )

        with patch("cloudinary.uploader.explicit") as mock_explicit:
            CloudinaryService().ensure_video_derivatives("users/x/posts/clip")

        mock_explicit.assert_called_once_with(
            "users/x/posts/clip",
            type="upload",
            resource_type="video",
            eager_async=True,
            eager=[
                {
                    "raw_transformation": VIDEO_EAGER_TRANSFORMATION,
                    "format": VIDEO_EAGER_FORMAT,
                },
                {
                    "raw_transformation": VIDEO_HLS_TRANSFORMATION,
                    "format": VIDEO_HLS_FORMAT,
                },
            ],
        )

    def test_empty_public_id_never_reaches_the_provider(self):
        from services.storage.cloudinary import CloudinaryService

        with patch("cloudinary.uploader.explicit") as mock_explicit:
            CloudinaryService().ensure_video_derivatives("")

        mock_explicit.assert_not_called()

    def test_provider_error_is_swallowed(self):
        from services.storage.cloudinary import CloudinaryService

        with patch("cloudinary.uploader.explicit", side_effect=Exception("boom")):
            # Must not raise — this runs on create paths.
            CloudinaryService().ensure_video_derivatives("users/x/posts/clip")


# =====================================================================
# Edit post — text fields only (content / visibility / sport / location)
# =====================================================================

class PostUpdateTests(APITestCase):
    """PATCH /posts/update — owner edits text fields; media is never touched."""

    def setUp(self):
        self.me = self._user("me", "Me")
        self.client.force_authenticate(user=self.me)

    def _user(self, username, name):
        user = User.objects.create_user(
            email=f"{username}@example.com", password="pass1234", username=username,
        )
        UserProfile.objects.create(user=user, name=name)
        return user

    def _org(self, username, name):
        org = Organization.objects.create(
            name=name, username=username, type=Organization.Type.CLUB
        )
        OrganizationProfile.objects.create(organization=org, logo="")
        return org

    def _patch(self, payload, org=None):
        headers = {}
        if org is not None:
            headers = {"HTTP_X_ACTOR_TYPE": "organization", "HTTP_X_ACTOR_ID": str(org.id)}
        return self.client.patch(UPDATE_URL, payload, format="json", **headers)

    # ── owner edits ──────────────────────────────────────────────

    def test_owner_updates_content_and_visibility(self):
        post = Post.objects.create(author_user=self.me, content="old")
        resp = self._patch({
            "post_id": str(post.id), "content": "new text", "visibility": "followers",
        })
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        post.refresh_from_db()
        self.assertEqual(post.content, "new text")
        self.assertEqual(post.visibility, "followers")

    def test_non_owner_cannot_update(self):
        other = self._user("o", "O")
        post = Post.objects.create(author_user=other, content="theirs")
        resp = self._patch({"post_id": str(post.id), "content": "hacked"})
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)
        post.refresh_from_db()
        self.assertEqual(post.content, "theirs")

    def test_clear_sport_and_location(self):
        sport = Sport.objects.create(name="Football", icon_name="mdi:soccer")
        post = Post.objects.create(
            author_user=self.me, content="x", sport=sport,
            location_name="Stadium", city="Kannur", latitude=11.0, longitude=75.0,
        )
        resp = self._patch({"post_id": str(post.id), "sport_id": None, "location": None})
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        post.refresh_from_db()
        self.assertIsNone(post.sport)
        self.assertIsNone(post.latitude)
        self.assertEqual(post.location_name, "")

    def test_location_absent_leaves_it_unchanged(self):
        post = Post.objects.create(
            author_user=self.me, content="x",
            location_name="Stadium", city="Kannur", latitude=11.0, longitude=75.0,
        )
        resp = self._patch({"post_id": str(post.id), "content": "edited"})
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        post.refresh_from_db()
        self.assertEqual(post.location_name, "Stadium")   # untouched
        self.assertEqual(post.content, "edited")

    def test_set_sport(self):
        sport = Sport.objects.create(name="Cricket", icon_name="mdi:cricket")
        post = Post.objects.create(author_user=self.me, content="x")
        resp = self._patch({"post_id": str(post.id), "sport_id": str(sport.id)})
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        post.refresh_from_db()
        self.assertEqual(post.sport_id, sport.id)

    def test_media_untouched_on_update(self):
        post = Post.objects.create(author_user=self.me, content="x")
        PostMedia.objects.create(
            post=post, file_url="https://cdn.example.com/x.jpg",
            public_id="users/x/posts/temp/x", media_type=PostMedia.MediaType.IMAGE, order=0,
        )
        resp = self._patch({"post_id": str(post.id), "content": "edited"})
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertEqual(post.media.count(), 1)

    def test_empty_content_without_media_rejected(self):
        post = Post.objects.create(author_user=self.me, content="something")
        resp = self._patch({"post_id": str(post.id), "content": "   "})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_org_actor_updates_org_post(self):
        org = self._org("dreamfc", "Dream FC")
        OrganizationMember.objects.create(
            organization=org, user=self.me, role=OrganizationMember.Role.OWNER
        )
        post = Post.objects.create(author_org=org, content="org old")
        resp = self._patch({"post_id": str(post.id), "content": "org new"}, org=org)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        post.refresh_from_db()
        self.assertEqual(post.content, "org new")


# =====================================================================
# Delete comment — comment author OR post owner
# =====================================================================

class CommentDeleteTests(APITestCase):
    """DELETE /posts/comments/delete — author or post owner; cascades replies."""

    def setUp(self):
        self.me = self._user("me", "Me")
        self.client.force_authenticate(user=self.me)

    def _user(self, username, name):
        user = User.objects.create_user(
            email=f"{username}@example.com", password="pass1234", username=username,
        )
        UserProfile.objects.create(user=user, name=name)
        return user

    def _org(self, username, name):
        org = Organization.objects.create(
            name=name, username=username, type=Organization.Type.CLUB
        )
        OrganizationProfile.objects.create(organization=org, logo="")
        return org

    def _comment(self, post, author, parent=None, reply_count=0):
        return Comment.objects.create(
            post=post, user=author, comment="c", parent=parent, reply_count=reply_count,
        )

    def _delete(self, comment_id, org=None):
        headers = {}
        if org is not None:
            headers = {"HTTP_X_ACTOR_TYPE": "organization", "HTTP_X_ACTOR_ID": str(org.id)}
        return self.client.delete(f"{COMMENT_DELETE_URL}?comment_id={comment_id}", **headers)

    # ── permission ───────────────────────────────────────────────

    def test_author_deletes_own_comment(self):
        post = Post.objects.create(author_user=self.me, content="p", comments_count=1)
        c = self._comment(post, self.me)
        resp = self._delete(c.id)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        c.refresh_from_db()
        self.assertTrue(c.is_deleted)
        post.refresh_from_db()
        self.assertEqual(post.comments_count, 0)

    def test_post_owner_deletes_others_comment(self):
        stranger = self._user("s", "S")
        post = Post.objects.create(author_user=self.me, content="p", comments_count=1)
        c = self._comment(post, stranger)
        resp = self._delete(c.id)   # I own the post
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        c.refresh_from_db()
        self.assertTrue(c.is_deleted)

    def test_unrelated_user_cannot_delete(self):
        owner = self._user("o", "O")
        stranger = self._user("s", "S")
        post = Post.objects.create(author_user=owner, content="p", comments_count=1)
        c = self._comment(post, stranger)
        resp = self._delete(c.id)   # me: neither owner nor author
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        c.refresh_from_db()
        self.assertFalse(c.is_deleted)

    # ── cascade + counters ───────────────────────────────────────

    def test_delete_top_level_cascades_replies(self):
        post = Post.objects.create(author_user=self.me, content="p", comments_count=3)
        root = self._comment(post, self.me, reply_count=2)
        r1 = self._comment(post, self.me, parent=root)
        r2 = self._comment(post, self.me, parent=root)
        resp = self._delete(root.id)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        for obj in (root, r1, r2):
            obj.refresh_from_db()
            self.assertTrue(obj.is_deleted)
        post.refresh_from_db()
        self.assertEqual(post.comments_count, 0)   # 3 − (1 + 2)

    def test_delete_reply_decrements_root_and_post(self):
        post = Post.objects.create(author_user=self.me, content="p", comments_count=2)
        root = self._comment(post, self.me, reply_count=1)
        reply = self._comment(post, self.me, parent=root)
        resp = self._delete(reply.id)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        reply.refresh_from_db(); root.refresh_from_db(); post.refresh_from_db()
        self.assertTrue(reply.is_deleted)
        self.assertEqual(root.reply_count, 0)
        self.assertEqual(post.comments_count, 1)

    def test_missing_comment_returns_404(self):
        import uuid as _uuid
        resp = self._delete(_uuid.uuid4())
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_org_owner_deletes_comment_on_org_post(self):
        org = self._org("dreamfc", "Dream FC")
        OrganizationMember.objects.create(
            organization=org, user=self.me, role=OrganizationMember.Role.OWNER
        )
        stranger = self._user("s", "S")
        post = Post.objects.create(author_org=org, content="p", comments_count=1)
        c = self._comment(post, stranger)
        resp = self._delete(c.id, org=org)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        c.refresh_from_db()
        self.assertTrue(c.is_deleted)


# =====================================================================
# Hashtags — parsed out of the body on write, diff-synced on edit
# =====================================================================

class PostHashtagTests(APITestCase):
    """
    Hashtag rows are DERIVED from post.content, never sent by the client.
    Create and update run the same diff-sync, and the search endpoint reads
    the lowercased stored form.
    """

    def setUp(self):
        self.me = User.objects.create_user(
            email="me@example.com", password="pass1234", username="me",
        )
        UserProfile.objects.create(user=self.me, name="Me")
        self.client.force_authenticate(user=self.me)

    def _tags(self, post):
        """Stored tag names for a post, sorted for stable comparison."""
        return sorted(
            PostHashtag.objects
            .filter(post=post)
            .values_list("hashtag__name", flat=True)
        )

    def _create(self, content):
        resp = self.client.post(CREATE_URL, {"content": content}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        return Post.objects.get(id=resp.data["data"]["post_id"])

    # ── create ───────────────────────────────────────────────────

    def test_create_extracts_unique_lowercased_tags(self):
        post = self._create("Big win #Football #football #GOATZA_2026")

        # "#Football" and "#football" are the SAME tag once folded — 2 rows, not 3.
        self.assertEqual(self._tags(post), ["football", "goatza_2026"])
        self.assertEqual(PostHashtag.objects.filter(post=post).count(), 2)

    def test_create_without_hashtags_creates_no_rows(self):
        post = self._create("just a plain caption")
        self.assertEqual(self._tags(post), [])

    # ── update ───────────────────────────────────────────────────

    def test_edit_content_diffs_the_rows(self):
        post = self._create("first #football #goatza")
        self.assertEqual(self._tags(post), ["football", "goatza"])

        resp = self.client.patch(
            UPDATE_URL,
            {"post_id": str(post.id), "content": "second #goatza #trials"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)

        # football dropped, trials added, goatza kept (not deleted+recreated).
        self.assertEqual(self._tags(post), ["goatza", "trials"])

    def test_edit_that_skips_content_leaves_rows_alone(self):
        post = self._create("keep me #football")
        row_ids = set(
            PostHashtag.objects.filter(post=post).values_list("id", flat=True)
        )

        resp = self.client.patch(
            UPDATE_URL,
            {"post_id": str(post.id), "visibility": "followers"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)

        self.assertEqual(self._tags(post), ["football"])
        # Same rows, untouched — the sync never ran.
        self.assertEqual(
            set(PostHashtag.objects.filter(post=post).values_list("id", flat=True)),
            row_ids,
        )

    # ── search ───────────────────────────────────────────────────

    def test_search_finds_stored_tag_case_insensitively(self):
        post = self._create("no keyword in the body at all #FootBall")

        resp = self.client.get(SEARCH_URL, {"q": "#FOOTBALL"})
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)

        ids = [str(r["id"]) for r in resp.data["data"]["results"]]
        self.assertIn(str(post.id), ids)

    # ── the parser itself ────────────────────────────────────────

    def test_extract_hashtags_caps_and_truncates(self):
        from posts.services.post_content_service import (
            MAX_HASHTAGS_PER_POST,
            extract_hashtags,
        )

        # 40 distinct tags → only the first 30 survive, in order.
        many = " ".join(f"#tag{i}" for i in range(40))
        tags = extract_hashtags(many)
        self.assertEqual(len(tags), MAX_HASHTAGS_PER_POST)
        self.assertEqual(tags[0], "tag0")
        self.assertEqual(tags[-1], "tag29")

        # A 60-char run yields a 50-char tag — the pattern stops at the cap
        # rather than rejecting the tag outright.
        long_tag = extract_hashtags("#" + ("a" * 60))
        self.assertEqual(long_tag, ["a" * 50])

        # Punctuation ends a tag; a bare "#" is not one.
        self.assertEqual(extract_hashtags("end of #season."), ["season"])
        self.assertEqual(extract_hashtags("# ## nothing here"), [])

    # ── backfill command ─────────────────────────────────────────

    def test_backfill_creates_rows_and_is_idempotent(self):
        from io import StringIO
        from django.core.management import call_command

        # A post from before the write path existed: content, but no rows.
        legacy = Post.objects.create(
            author_user=self.me, content="old glory #football #kerala"
        )
        untagged = Post.objects.create(author_user=self.me, content="no tags here")
        self.assertEqual(self._tags(legacy), [])

        call_command("backfill_post_hashtags", stdout=StringIO())
        self.assertEqual(self._tags(legacy), ["football", "kerala"])
        self.assertEqual(self._tags(untagged), [])

        after_first = PostHashtag.objects.count()

        # Re-running must be a no-op, not a duplicate-key crash.
        call_command("backfill_post_hashtags", stdout=StringIO())
        self.assertEqual(PostHashtag.objects.count(), after_first)
        self.assertEqual(self._tags(legacy), ["football", "kerala"])


# =====================================================================
# Mentions — @handles resolved to users OR orgs, notified once, listed
# =====================================================================

class PostMentionTests(APITestCase):
    """
    Mentions are parsed out of the body and resolved against two SEPARATE
    username tables (users win collisions). Rows are always created; the
    NOTIFICATION is what visibility gates.
    """

    def setUp(self):
        self.me = self._user("me", "Me")
        self.client.force_authenticate(user=self.me)

    # ── factories ────────────────────────────────────────────────

    def _user(self, username, name):
        user = User.objects.create_user(
            email=f"{username}@example.com", password="pass1234", username=username,
        )
        UserProfile.objects.create(user=user, name=name)
        return user

    def _org(self, username, name, member=None):
        org = Organization.objects.create(
            name=name, username=username, type=Organization.Type.CLUB
        )
        OrganizationProfile.objects.create(
            organization=org, logo=f"https://cdn.example.com/{username}.png"
        )
        if member is not None:
            OrganizationMember.objects.create(
                organization=org, user=member, role=OrganizationMember.Role.OWNER
            )
        return org

    def _org_headers(self, org):
        return {
            "HTTP_X_ACTOR_TYPE": "organization",
            "HTTP_X_ACTOR_ID": str(org.id),
        }

    def _create(self, content, visibility="public", org=None):
        """Create a post through the API, running on_commit callbacks."""
        headers = self._org_headers(org) if org is not None else {}
        with self.captureOnCommitCallbacks(execute=True):
            resp = self.client.post(
                CREATE_URL,
                {"content": content, "visibility": visibility},
                format="json",
                **headers,
            )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        return Post.objects.get(id=resp.data["data"]["post_id"])

    def _edit(self, post, content, org=None):
        headers = self._org_headers(org) if org is not None else {}
        with self.captureOnCommitCallbacks(execute=True):
            resp = self.client.patch(
                UPDATE_URL,
                {"post_id": str(post.id), "content": content},
                format="json",
                **headers,
            )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        return resp

    def _mention_targets(self, post):
        rows = PostMention.objects.filter(post=post).select_related(
            "mentioned_user", "mentioned_org"
        )
        return sorted(
            (row.mentioned_user.username if row.mentioned_user_id
             else row.mentioned_org.username)
            for row in rows
        )

    def _mention_notifications(self, **recipient):
        return Notification.objects.filter(
            type=Notification.Type.MENTION, **recipient
        )

    # ── extraction + resolution ──────────────────────────────────

    def test_create_resolves_users_and_orgs_and_ignores_unknown(self):
        rahul = self._user("rahul10", "Rahul")
        kochi = self._org("kochifc", "Kochi FC")

        post = self._create("gg @rahul10 @KochiFC @nosuchname")

        self.assertEqual(self._mention_targets(post), ["kochifc", "rahul10"])
        self.assertEqual(PostMention.objects.filter(post=post).count(), 2)

        row_user = PostMention.objects.get(post=post, mentioned_user__isnull=False)
        row_org = PostMention.objects.get(post=post, mentioned_org__isnull=False)
        self.assertEqual(row_user.mentioned_user_id, rahul.id)
        self.assertEqual(row_org.mentioned_org_id, kochi.id)

    def test_username_collision_resolves_to_the_user(self):
        # The same handle exists on BOTH tables — documented policy: user wins.
        twin_user = self._user("dreamfc", "Dream Person")
        self._org("dreamfc", "Dream FC")

        post = self._create("shoutout @dreamfc")

        rows = PostMention.objects.filter(post=post)
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.first().mentioned_user_id, twin_user.id)
        self.assertIsNone(rows.first().mentioned_org_id)

    def test_trailing_punctuation_is_not_part_of_the_handle(self):
        self._org("kochifc", "Kochi FC")

        post = self._create("great game @kochifc.")

        self.assertEqual(self._mention_targets(post), ["kochifc"])

    # ── notifications ────────────────────────────────────────────

    def test_edit_notifies_only_the_newly_added_mention(self):
        rahul = self._user("rahul10", "Rahul")
        newguy = self._user("newguy", "New Guy")

        post = self._create("first @rahul10")
        self.assertEqual(self._mention_notifications(recipient_user=rahul).count(), 1)

        self._edit(post, "first @rahul10 and @newguy")

        # Rows diffed correctly...
        self.assertEqual(self._mention_targets(post), ["newguy", "rahul10"])
        # ...and only the new person heard about it.
        self.assertEqual(self._mention_notifications(recipient_user=newguy).count(), 1)
        self.assertEqual(self._mention_notifications(recipient_user=rahul).count(), 1)

    def test_edit_removing_a_mention_drops_the_row(self):
        self._user("rahul10", "Rahul")
        self._user("newguy", "New Guy")

        post = self._create("first @rahul10")
        self._edit(post, "second @newguy")

        self.assertEqual(self._mention_targets(post), ["newguy"])

    def test_self_mention_creates_a_row_but_no_notification(self):
        post = self._create("talking about @me here")

        self.assertEqual(self._mention_targets(post), ["me"])
        self.assertEqual(self._mention_notifications(recipient_user=self.me).count(), 0)

    def test_followers_only_post_notifies_followers_only(self):
        follower = self._user("follower", "Follower")
        stranger = self._user("stranger", "Stranger")
        Follow.objects.create(follower_user=follower, following_user=self.me)

        post = self._create(
            "private drills @follower @stranger", visibility="followers"
        )

        # Rows exist for BOTH — visibility gates the notification, not the row.
        self.assertEqual(self._mention_targets(post), ["follower", "stranger"])
        self.assertEqual(
            self._mention_notifications(recipient_user=follower).count(), 1
        )
        self.assertEqual(
            self._mention_notifications(recipient_user=stranger).count(), 0
        )

    def test_org_authored_post_notifies_a_mentioned_user(self):
        org = self._org("dreamfc", "Dream FC", member=self.me)
        rahul = self._user("rahul10", "Rahul")

        post = self._create("welcome @rahul10", org=org)

        self.assertEqual(post.author_org_id, org.id)
        notification = self._mention_notifications(recipient_user=rahul).first()
        self.assertIsNotNone(notification)
        self.assertEqual(notification.actor_org_id, org.id)

    def test_user_authored_post_notifies_a_mentioned_org(self):
        kochi = self._org("kochifc", "Kochi FC")

        post = self._create("trials at @kochifc")

        notification = self._mention_notifications(recipient_org=kochi).first()
        self.assertIsNotNone(notification)
        self.assertEqual(notification.actor_user_id, self.me.id)
        self.assertEqual(notification.post_id, post.id)

    # ── payload ──────────────────────────────────────────────────

    def test_mention_payload_builds_for_user_and_org_recipients(self):
        from notifications.services.notification_service import (
            build_notification_payload,
        )

        rahul = self._user("rahul10", "Rahul")
        kochi = self._org("kochifc", "Kochi FC")
        post = self._create("hello @rahul10 @kochifc")

        # One post, two destinations: the URL is resolved in the RECIPIENT's
        # route space, so the org's copy stays inside the admin area rather than
        # switching the reader back to their personal account. (Plural /posts/ —
        # /post/<id> is not a route.)
        cases = (
            ({"recipient_user": rahul}, f"/posts/{post.id}"),
            (
                {"recipient_org": kochi},
                f"/organization/admin/{kochi.id}/posts/{post.id}",
            ),
        )

        for recipient, expected_url in cases:
            notification = self._mention_notifications(**recipient).first()
            self.assertIsNotNone(notification, recipient)

            payload = build_notification_payload(notification)
            self.assertEqual(payload["type"], "mention")
            self.assertEqual(payload["title"], "Me mentioned you in a post")
            self.assertEqual(payload["url"], expected_url)
            self.assertEqual(payload["target_id"], str(post.id))

    def test_grouped_text_renders_for_mention(self):
        from notifications.services.grouping_service import (
            NotificationGroupingService,
        )

        rahul = self._user("rahul10", "Rahul")
        self._create("hello @rahul10")

        notifications = list(
            self._mention_notifications(recipient_user=rahul)
            .select_related("actor_user__profile", "post")
        )
        grouped = NotificationGroupingService.group_notifications(notifications)

        self.assertEqual(len(grouped), 1)
        self.assertEqual(grouped[0]["text"], "Me mentioned you in a post")

    # ── mentions/my ──────────────────────────────────────────────

    def test_my_mentions_is_actor_scoped(self):
        org = self._org("dreamfc", "Dream FC", member=self.me)
        author = self._user("author", "Author")

        # Someone mentions the USER in one post and the ORG in another.
        self.client.force_authenticate(user=author)
        user_post = self._create("hey @me")
        org_post = self._create("hey @dreamfc")

        self.client.force_authenticate(user=self.me)

        as_user = self.client.get(MY_MENTIONS_URL)
        self.assertEqual(as_user.status_code, status.HTTP_200_OK, as_user.data)
        user_ids = [r["id"] for r in as_user.data["data"]["results"]]
        self.assertEqual(user_ids, [str(user_post.id)])

        as_org = self.client.get(MY_MENTIONS_URL, **self._org_headers(org))
        self.assertEqual(as_org.status_code, status.HTTP_200_OK, as_org.data)
        org_ids = [r["id"] for r in as_org.data["data"]["results"]]
        self.assertEqual(org_ids, [str(org_post.id)])

    def test_my_mentions_excludes_deleted_posts_and_exposes_mentions(self):
        author = self._user("author", "Author")
        self.client.force_authenticate(user=author)
        live = self._create("hey @me")
        gone = self._create("also @me")
        gone.is_deleted = True
        gone.save(update_fields=["is_deleted"])

        self.client.force_authenticate(user=self.me)
        resp = self.client.get(MY_MENTIONS_URL)

        results = resp.data["data"]["results"]
        self.assertEqual([r["id"] for r in results], [str(live.id)])
        # The serializer field the client linkifies with.
        self.assertEqual(
            results[0]["mentions"], [{"username": "me", "type": "user"}]
        )

    # ── suggest ──────────────────────────────────────────────────

    def test_suggest_returns_prefix_matches_for_both_types(self):
        self._user("rahul10", "Rahul")
        self._user("rahulraj", "Rahul Raj")
        self._user("notrahul", "Not Rahul")     # contains, does not start with
        self._org("rahulfc", "Rahul FC")
        self._org("otherfc", "Other FC")

        resp = self.client.get(MENTION_SUGGEST_URL, {"q": "rahul"})
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)

        data = resp.data["data"]
        self.assertEqual(
            sorted(u["username"] for u in data["users"]), ["rahul10", "rahulraj"]
        )
        self.assertEqual(
            [o["username"] for o in data["organizations"]], ["rahulfc"]
        )
        # Org avatars come back under `logo`, the actual model field.
        self.assertTrue(data["organizations"][0]["logo"])
        self.assertIn("profile_photo", data["users"][0])

    def test_suggest_tolerates_a_leading_at_and_empty_query(self):
        self._user("rahul10", "Rahul")

        typed = self.client.get(MENTION_SUGGEST_URL, {"q": "@rahul"})
        self.assertEqual(
            [u["username"] for u in typed.data["data"]["users"]], ["rahul10"]
        )

        empty = self.client.get(MENTION_SUGGEST_URL, {"q": "  "})
        self.assertEqual(empty.status_code, status.HTTP_200_OK)
        self.assertEqual(empty.data["data"], {"users": [], "organizations": []})

    def test_suggest_requires_authentication(self):
        self.client.force_authenticate(user=None)
        resp = self.client.get(MENTION_SUGGEST_URL, {"q": "rahul"})
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    # ── the parser itself ────────────────────────────────────────

    def test_extract_mention_usernames_folds_case_and_caps(self):
        from posts.services.post_content_service import (
            MAX_MENTIONS_PER_POST,
            extract_mention_usernames,
        )

        # Matched case-insensitively and LOWERCASED on capture: handles are
        # stored lowercase now that users and organizations share one
        # namespace, so "@Rahul10" and "@rahul10" are one person and one row.
        self.assertEqual(
            extract_mention_usernames("@Rahul10 hi @rahul10"), ["rahul10"]
        )

        many = " ".join(f"@user{i}" for i in range(30))
        self.assertEqual(
            len(extract_mention_usernames(many)), MAX_MENTIONS_PER_POST
        )

        # The dot is no longer a handle character for EITHER actor type, so
        # "@kochi.fc." is the handle "kochi" followed by prose.
        self.assertEqual(extract_mention_usernames("@kochi.fc."), ["kochi"])
        self.assertEqual(extract_mention_usernames("email me@ x"), [])


# =====================================================================
# Saved posts — per ACTOR, private to the saver
# =====================================================================

class SavedPostTests(APITestCase):
    """
    A save belongs to the actor that made it: a person and an org they run
    keep completely separate lists of the same post.
    """

    def setUp(self):
        self.me = self._user("me", "Me")
        self.author = self._user("author", "Author")
        self.org = self._org("dreamfc", "Dream FC", member=self.me)
        self.client.force_authenticate(user=self.me)

    # ── factories ────────────────────────────────────────────────

    def _user(self, username, name):
        user = User.objects.create_user(
            email=f"{username}@example.com", password="pass1234", username=username,
        )
        UserProfile.objects.create(user=user, name=name)
        return user

    def _org(self, username, name, member=None):
        org = Organization.objects.create(
            name=name, username=username, type=Organization.Type.CLUB
        )
        OrganizationProfile.objects.create(organization=org, logo="")
        if member is not None:
            OrganizationMember.objects.create(
                organization=org, user=member, role=OrganizationMember.Role.OWNER
            )
        return org

    def _org_headers(self):
        return {
            "HTTP_X_ACTOR_TYPE": "organization",
            "HTTP_X_ACTOR_ID": str(self.org.id),
        }

    def _post(self, content="p"):
        return Post.objects.create(author_user=self.author, content=content)

    def _toggle(self, post_id, as_org=False):
        headers = self._org_headers() if as_org else {}
        return self.client.post(
            SAVE_URL, {"post_id": str(post_id)}, format="json", **headers
        )

    def _saved_list(self, as_org=False):
        headers = self._org_headers() if as_org else {}
        return self.client.get(SAVED_LIST_URL, **headers)

    def _list_row(self, post_id):
        """The post as the main list serializes it, for the annotation checks."""
        resp = self.client.get(LIST_URL, {"post_id": str(post_id)})
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        return resp.data["data"]["results"][0]

    # ── toggle as a user ─────────────────────────────────────────

    def test_toggle_as_user_saves_then_unsaves(self):
        post = self._post()

        on = self._toggle(post.id)
        self.assertEqual(on.status_code, status.HTTP_200_OK, on.data)
        self.assertTrue(on.data["data"]["is_saved"])
        self.assertEqual(on.data["data"]["post_id"], str(post.id))

        row = SavedPost.objects.get(post=post)
        self.assertEqual(row.user_id, self.me.id)
        self.assertIsNone(row.org_id)
        self.assertTrue(self._list_row(post.id)["is_saved"])

        off = self._toggle(post.id)
        self.assertEqual(off.status_code, status.HTTP_200_OK, off.data)
        self.assertFalse(off.data["data"]["is_saved"])
        self.assertFalse(SavedPost.objects.filter(post=post).exists())
        self.assertFalse(self._list_row(post.id)["is_saved"])

    # ── the two actors are independent ───────────────────────────

    def test_org_save_is_a_separate_row_and_list(self):
        post = self._post()

        self._toggle(post.id)                 # as me
        self._toggle(post.id, as_org=True)    # as the org

        self.assertEqual(SavedPost.objects.filter(post=post).count(), 2)
        self.assertTrue(
            SavedPost.objects.filter(post=post, user=self.me, org__isnull=True).exists()
        )
        self.assertTrue(
            SavedPost.objects.filter(post=post, org=self.org, user__isnull=True).exists()
        )

        # Unsaving as the org leaves the person's save untouched.
        self._toggle(post.id, as_org=True)
        self.assertTrue(SavedPost.objects.filter(post=post, user=self.me).exists())
        self.assertFalse(SavedPost.objects.filter(post=post, org=self.org).exists())

    def test_saved_list_is_actor_scoped(self):
        mine = self._post("mine")
        theirs = self._post("org's")

        self._toggle(mine.id)
        self._toggle(theirs.id, as_org=True)

        as_user = self._saved_list()
        self.assertEqual(as_user.status_code, status.HTTP_200_OK, as_user.data)
        self.assertEqual(
            [r["id"] for r in as_user.data["data"]["results"]], [str(mine.id)]
        )

        as_org = self._saved_list(as_org=True)
        self.assertEqual(as_org.status_code, status.HTTP_200_OK, as_org.data)
        self.assertEqual(
            [r["id"] for r in as_org.data["data"]["results"]], [str(theirs.id)]
        )

    def test_saved_list_is_newest_saved_first(self):
        first = self._post("first")
        second = self._post("second")
        third = self._post("third")

        # Saved out of post order — the LIST order must follow the saves.
        self._toggle(second.id)
        self._toggle(third.id)
        self._toggle(first.id)

        resp = self._saved_list()
        self.assertEqual(
            [r["id"] for r in resp.data["data"]["results"]],
            [str(first.id), str(third.id), str(second.id)],
        )

    def test_saved_list_carries_is_saved_true(self):
        post = self._post()
        self._toggle(post.id)

        resp = self._saved_list()
        row = resp.data["data"]["results"][0]
        # Trivially true here, but an absent/False flag renders an empty
        # bookmark on the saved list itself.
        self.assertTrue(row["is_saved"])

    # ── annotation across viewers ────────────────────────────────

    def test_is_saved_is_false_for_a_non_saver(self):
        post = self._post()
        self._toggle(post.id)

        self.client.force_authenticate(user=self.author)
        self.assertFalse(self._list_row(post.id)["is_saved"])

    # ── constraints + errors ─────────────────────────────────────

    def test_row_with_both_user_and_org_is_rejected(self):
        from django.db.utils import IntegrityError

        post = self._post()
        with self.assertRaises(IntegrityError):
            SavedPost.objects.create(post=post, user=self.me, org=self.org)

    def test_row_with_neither_user_nor_org_is_rejected(self):
        from django.db.utils import IntegrityError

        post = self._post()
        with self.assertRaises(IntegrityError):
            SavedPost.objects.create(post=post)

    def test_saving_a_deleted_post_returns_404(self):
        post = self._post()
        post.is_deleted = True
        post.save(update_fields=["is_deleted"])

        resp = self._toggle(post.id)
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND, resp.data)
        self.assertFalse(SavedPost.objects.filter(post=post).exists())

    def test_saving_an_unknown_post_returns_404(self):
        import uuid as _uuid

        resp = self._toggle(_uuid.uuid4())
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND, resp.data)

    def test_missing_post_id_returns_400(self):
        resp = self.client.post(SAVE_URL, {}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST, resp.data)

    def test_unsaving_hides_the_post_from_the_saved_list(self):
        post = self._post()
        self._toggle(post.id)
        self._toggle(post.id)

        resp = self._saved_list()
        self.assertEqual(resp.data["data"]["results"], [])
