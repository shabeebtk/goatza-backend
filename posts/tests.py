from unittest.mock import patch

from django.test import override_settings
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import User, UserProfile
from organization.models import (
    Organization,
    OrganizationProfile,
    OrganizationMember,
)
from posts.models import Post, PostMedia, Like, Comment, Hashtag, PostHashtag
from sports.models import Sport

SEARCH_URL = "/posts/search"
LIST_URL = "/posts/list"
CREATE_URL = "/posts/create"
UPDATE_URL = "/posts/update"
COMMENT_DELETE_URL = "/posts/comments/delete"


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

    @patch("services.storage.cloudinary.CloudinaryService.get_media_metadata")
    def test_server_duration_overrides_client_for_video(self, mock_meta):
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
