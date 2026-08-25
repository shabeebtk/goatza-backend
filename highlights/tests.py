from datetime import timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.db.models import F
from django.test import override_settings
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import User, UserProfile
from connections.models import Follow
from highlights.models import Highlight, HighlightView
from organization.models import (
    Organization,
    OrganizationMember,
    OrganizationProfile,
)
from posts.models import Post, PostMedia

BASE_URL = "/highlights"
CREATE_URL = f"{BASE_URL}/create"
REORDER_URL = f"{BASE_URL}/reorder"


def list_url(username):
    return f"{BASE_URL}/user/{username}"


def detail_url(highlight_id):
    return f"{BASE_URL}/{highlight_id}"


def view_url(highlight_id):
    return f"{BASE_URL}/{highlight_id}/view"


@override_settings(
    # Six users per test; the real hasher makes setUp dominate the run.
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"]
)
class HighlightAPITestCase(APITestCase):
    """
    Shared cast for the highlights API: one player who owns the rail, plus one
    viewer of every kind the visibility matrix cares about.
    """

    def setUp(self):
        cache.clear()

        self.owner = self._user("owner", User.Role.PLAYER)
        self.follower = self._user("follower", User.Role.PLAYER)
        self.stranger = self._user("stranger", User.Role.PLAYER)
        self.scout = self._user("scout", User.Role.SCOUT)
        self.coach = self._user("coach", User.Role.COACH)
        self.orguser = self._user("orguser", User.Role.ORG_USER)

        self.org = self._org("dreamfc", "Dream FC")
        # the org actor is only honored for a verified member
        OrganizationMember.objects.create(
            organization=self.org,
            user=self.orguser,
            role=OrganizationMember.Role.OWNER,
        )

        Follow.objects.create(
            follower_user=self.follower,
            following_user=self.owner,
        )

    # ── factories ────────────────────────────────────────────────

    def _user(self, username, role):
        user = User.objects.create_user(
            email=f"{username}@example.com",
            password="pass1234",
            username=username,
            role=role,
        )
        UserProfile.objects.create(user=user, name=username.title())
        return user

    def _org(self, username, name):
        org = Organization.objects.create(
            name=name,
            username=username,
            type=Organization.Type.CLUB,
        )
        OrganizationProfile.objects.create(organization=org)
        return org

    def _highlight(self, user=None, visibility=None, order=0, **kwargs):
        user = user or self.owner
        return Highlight.objects.create(
            user=user,
            title=kwargs.pop("title", f"clip {order}"),
            file_url=kwargs.pop(
                "file_url",
                f"https://media.goatza.test/{order}.mp4",
            ),
            public_id=kwargs.pop("public_id", f"goatza/highlights/{user.username}-{order}"),
            visibility=visibility or Highlight.Visibility.FOLLOWERS_AND_RECRUITERS,
            order=order,
            **kwargs,
        )

    def _video_post(self, author=None, **media_kwargs):
        """A post with one video attachment — the promote-mode source."""
        author = author or self.owner
        post = Post.objects.create(author_user=author, content="match day")
        media = PostMedia.objects.create(
            post=post,
            file_url=media_kwargs.pop(
                "file_url",
                "https://media.goatza.test/goal.mp4",
            ),
            public_id=media_kwargs.pop("public_id", "goatza/posts/goal"),
            media_type=media_kwargs.pop("media_type", PostMedia.MediaType.VIDEO),
            thumbnail_url=media_kwargs.pop(
                "thumbnail_url",
                "https://media.goatza.test/goal.jpg",
            ),
            duration=media_kwargs.pop("duration", 25),
            width=media_kwargs.pop("width", 1080),
            height=media_kwargs.pop("height", 1920),
            **media_kwargs,
        )
        return post, media

    def _three_levels(self):
        """One clip per visibility level, in rail order."""
        return (
            self._highlight(visibility=Highlight.Visibility.EVERYONE, order=0, title="open"),
            self._highlight(
                visibility=Highlight.Visibility.FOLLOWERS_AND_RECRUITERS,
                order=1,
                title="followers",
            ),
            self._highlight(
                visibility=Highlight.Visibility.RECRUITERS_ONLY,
                order=2,
                title="recruiters",
            ),
        )

    # ── request helpers ──────────────────────────────────────────

    def _auth(self, user, org=None):
        self.client.force_authenticate(user=user)
        if org is None:
            return {}
        return {
            "HTTP_X_ACTOR_TYPE": "organization",
            "HTTP_X_ACTOR_ID": str(org.id),
        }

    def _list(self, viewer, username=None, org=None):
        headers = self._auth(viewer, org)
        return self.client.get(list_url(username or self.owner.username), **headers)

    def _create(self, actor_user, payload=None, org=None):
        headers = self._auth(actor_user, org)
        body = {
            "file_url": "https://media.goatza.test/new.mp4",
            "public_id": "goatza/highlights/new",
        }
        body.update(payload or {})
        return self.client.post(CREATE_URL, body, format="json", **headers)

    def _patch(self, actor_user, highlight_id, payload, org=None):
        headers = self._auth(actor_user, org)
        return self.client.patch(
            detail_url(highlight_id), payload, format="json", **headers
        )

    def _delete(self, actor_user, highlight_id, org=None):
        headers = self._auth(actor_user, org)
        return self.client.delete(detail_url(highlight_id), **headers)

    def _reorder(self, actor_user, ordered_ids, org=None):
        headers = self._auth(actor_user, org)
        return self.client.put(
            REORDER_URL,
            {"ordered_ids": [str(i) for i in ordered_ids]},
            format="json",
            **headers,
        )

    def _view(self, actor_user, highlight_id, org=None):
        headers = self._auth(actor_user, org)
        return self.client.post(view_url(highlight_id), {}, format="json", **headers)

    # ── assertions ───────────────────────────────────────────────

    def _titles(self, resp):
        return [row["title"] for row in resp.data["data"]["results"]]


class HighlightVisibilityMatrixTests(HighlightAPITestCase):
    """
    Per-clip visibility (§1): everyone → all; followers_and_recruiters →
    recruiters and followers; recruiters_only → recruiters. Owner sees all.
    """

    def setUp(self):
        super().setUp()
        self.open_clip, self.followers_clip, self.recruiters_clip = self._three_levels()

    def test_owner_sees_every_clip_and_the_owner_only_fields(self):
        resp = self._list(self.owner)

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(self._titles(resp), ["open", "followers", "recruiters"])
        self.assertTrue(resp.data["data"]["is_owner"])

        first = resp.data["data"]["results"][0]
        self.assertIn("visibility", first)
        self.assertIn("views_count", first)

    def test_stranger_sees_only_the_everyone_clip(self):
        resp = self._list(self.stranger)

        self.assertEqual(self._titles(resp), ["open"])
        self.assertFalse(resp.data["data"]["is_owner"])

    def test_follower_sees_everyone_and_followers_clips(self):
        resp = self._list(self.follower)

        self.assertEqual(self._titles(resp), ["open", "followers"])

    def test_scout_sees_every_clip(self):
        self.assertEqual(
            self._titles(self._list(self.scout)),
            ["open", "followers", "recruiters"],
        )

    def test_coach_sees_every_clip(self):
        self.assertEqual(
            self._titles(self._list(self.coach)),
            ["open", "followers", "recruiters"],
        )

    def test_org_actor_sees_every_clip(self):
        resp = self._list(self.orguser, org=self.org)

        self.assertEqual(self._titles(resp), ["open", "followers", "recruiters"])
        # acting as an org is never "the owner", even for the owner's own org
        self.assertFalse(resp.data["data"]["is_owner"])

    def test_owner_acting_as_org_is_a_recruiter_not_the_owner(self):
        OrganizationMember.objects.create(
            organization=self.org,
            user=self.owner,
            role=OrganizationMember.Role.OWNER,
        )
        resp = self._list(self.owner, org=self.org)

        self.assertEqual(self._titles(resp), ["open", "followers", "recruiters"])
        self.assertFalse(resp.data["data"]["is_owner"])
        self.assertNotIn("visibility", resp.data["data"]["results"][0])

    def test_non_owner_never_sees_visibility_or_views_count(self):
        for viewer in (self.stranger, self.follower, self.scout, self.coach):
            row = self._list(viewer).data["data"]["results"][0]
            self.assertNotIn("visibility", row, viewer.username)
            self.assertNotIn("views_count", row, viewer.username)

    def test_list_requires_authentication(self):
        self.client.force_authenticate(user=None)
        resp = self.client.get(list_url(self.owner.username))

        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_unknown_username_is_404(self):
        resp = self._list(self.stranger, username="ghost")

        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)


class HighlightWritePermissionTests(HighlightAPITestCase):
    """Writes are player-only, acting as themselves (§1)."""

    def setUp(self):
        super().setUp()
        self.clip = self._highlight(order=0)

    def test_player_can_create_update_reorder_and_delete(self):
        resp = self._create(self.owner, {"title": "New clip"})
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        new_id = resp.data["data"]["id"]

        resp = self._patch(self.owner, self.clip.id, {"title": "Renamed"})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        resp = self._reorder(self.owner, [new_id, self.clip.id])
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        resp = self._delete(self.owner, self.clip.id)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_scout_and_coach_are_forbidden_on_every_write(self):
        for actor_user in (self.scout, self.coach):
            with self.subTest(role=actor_user.role):
                self.assertEqual(
                    self._create(actor_user).status_code,
                    status.HTTP_403_FORBIDDEN,
                )
                self.assertEqual(
                    self._patch(actor_user, self.clip.id, {"title": "x"}).status_code,
                    status.HTTP_403_FORBIDDEN,
                )
                self.assertEqual(
                    self._reorder(actor_user, [self.clip.id]).status_code,
                    status.HTTP_403_FORBIDDEN,
                )
                self.assertEqual(
                    self._delete(actor_user, self.clip.id).status_code,
                    status.HTTP_403_FORBIDDEN,
                )

    def test_org_actor_is_forbidden_on_every_write(self):
        self.assertEqual(
            self._create(self.orguser, org=self.org).status_code,
            status.HTTP_403_FORBIDDEN,
        )
        self.assertEqual(
            self._patch(
                self.orguser, self.clip.id, {"title": "x"}, org=self.org
            ).status_code,
            status.HTTP_403_FORBIDDEN,
        )
        self.assertEqual(
            self._reorder(self.orguser, [self.clip.id], org=self.org).status_code,
            status.HTTP_403_FORBIDDEN,
        )
        self.assertEqual(
            self._delete(self.orguser, self.clip.id, org=self.org).status_code,
            status.HTTP_403_FORBIDDEN,
        )

    def test_forbidden_writes_change_nothing(self):
        self._create(self.scout)
        self._patch(self.scout, self.clip.id, {"title": "hacked"})

        self.clip.refresh_from_db()
        self.assertEqual(self.clip.title, "clip 0")
        self.assertEqual(Highlight.objects.count(), 1)

    def test_another_player_cannot_touch_someone_elses_clip(self):
        resp = self._patch(self.stranger, self.clip.id, {"title": "mine now"})

        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("your own highlights", resp.data["message"])
        self.assertEqual(
            self._delete(self.stranger, self.clip.id).status_code,
            status.HTTP_403_FORBIDDEN,
        )

    def test_permission_is_checked_before_the_body(self):
        """A non-player gets 403, never a critique of a payload they may not send."""
        resp = self._create(self.scout, {"file_url": "not-a-url"})

        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_writes_require_authentication(self):
        self.client.force_authenticate(user=None)

        self.assertEqual(
            self.client.post(CREATE_URL, {}, format="json").status_code,
            status.HTTP_401_UNAUTHORIZED,
        )


class HighlightCapTests(HighlightAPITestCase):
    """Ten clips per player, ninety seconds per clip (§1)."""

    def test_eleventh_highlight_is_rejected(self):
        for i in range(10):
            self.assertEqual(
                self._create(
                    self.owner,
                    {"public_id": f"goatza/highlights/{i}", "title": f"clip {i}"},
                ).status_code,
                status.HTTP_201_CREATED,
            )

        resp = self._create(self.owner, {"public_id": "goatza/highlights/eleven"})

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            resp.data["message"],
            "You already have 10 highlights. Delete one to add another.",
        )
        self.assertEqual(Highlight.objects.filter(user=self.owner).count(), 10)

    def test_clip_longer_than_ninety_seconds_is_rejected(self):
        resp = self._create(self.owner, {"duration": 91})

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            resp.data["message"],
            "Highlights can be at most 90 seconds long. This clip is 91 seconds.",
        )
        self.assertFalse(Highlight.objects.exists())

    def test_exactly_ninety_seconds_is_allowed(self):
        resp = self._create(self.owner, {"duration": 90})

        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

    def test_new_clip_is_appended_to_the_end_of_the_rail(self):
        self._highlight(order=0)
        self._highlight(order=1)

        resp = self._create(self.owner, {"title": "last"})

        self.assertEqual(resp.data["data"]["order"], 2)


class HighlightPromoteTests(HighlightAPITestCase):
    """Promotion copies the media so the clip outlives the post (§1)."""

    def test_promote_copies_every_media_field_and_sets_source_post(self):
        post, media = self._video_post()

        resp = self._create(self.owner, {"source_media_id": str(media.id)})

        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        highlight = Highlight.objects.get(id=resp.data["data"]["id"])

        self.assertEqual(highlight.file_url, media.file_url)
        self.assertEqual(highlight.public_id, media.public_id)
        self.assertEqual(highlight.thumbnail_url, media.thumbnail_url)
        self.assertEqual(highlight.duration, media.duration)
        self.assertEqual(highlight.width, media.width)
        self.assertEqual(highlight.height, media.height)
        self.assertEqual(highlight.source_post_id, post.id)

    def test_promoted_clip_survives_a_soft_deleted_post_unchanged(self):
        post, media = self._video_post()
        resp = self._create(self.owner, {"source_media_id": str(media.id)})
        highlight = Highlight.objects.get(id=resp.data["data"]["id"])

        post.is_deleted = True
        post.save(update_fields=["is_deleted"])
        highlight.refresh_from_db()

        self.assertFalse(highlight.is_deleted)
        self.assertEqual(highlight.file_url, media.file_url)
        self.assertEqual(highlight.public_id, media.public_id)
        self.assertEqual(highlight.thumbnail_url, media.thumbnail_url)
        self.assertEqual(highlight.duration, media.duration)
        self.assertEqual(highlight.source_post_id, post.id)
        # still listed and still playable
        self.assertEqual(len(self._list(self.owner).data["data"]["results"]), 1)

    def test_promoted_clip_survives_a_hard_deleted_post_with_source_nulled(self):
        post, media = self._video_post()
        resp = self._create(self.owner, {"source_media_id": str(media.id)})
        highlight = Highlight.objects.get(id=resp.data["data"]["id"])
        file_url = highlight.file_url

        post.delete()
        highlight.refresh_from_db()

        self.assertIsNone(highlight.source_post_id)
        self.assertEqual(highlight.file_url, file_url)

    def test_cannot_promote_another_players_media(self):
        _, media = self._video_post(author=self.stranger)

        resp = self._create(self.owner, {"source_media_id": str(media.id)})

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            resp.data["message"], "You can only add media from your own posts."
        )
        self.assertFalse(Highlight.objects.exists())

    def test_cannot_promote_an_image(self):
        _, media = self._video_post(
            media_type=PostMedia.MediaType.IMAGE,
            file_url="https://media.goatza.test/team.jpg",
            duration=None,
            width=None,
            height=None,
        )

        resp = self._create(self.owner, {"source_media_id": str(media.id)})

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            resp.data["message"], "Only videos can be added to highlights."
        )

    def test_cannot_promote_from_an_already_deleted_post(self):
        post, media = self._video_post()
        post.is_deleted = True
        post.save(update_fields=["is_deleted"])

        resp = self._create(self.owner, {"source_media_id": str(media.id)})

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(resp.data["message"], "That post has been deleted.")

    def test_promoted_clip_over_ninety_seconds_is_rejected(self):
        _, media = self._video_post(duration=120)

        resp = self._create(self.owner, {"source_media_id": str(media.id)})

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("90 seconds", resp.data["message"])


class HighlightReorderTests(HighlightAPITestCase):
    """The rail is renumbered 0..n-1, all at once or not at all (§2)."""

    def setUp(self):
        super().setUp()
        self.a = self._highlight(order=0, title="a")
        self.b = self._highlight(order=1, title="b")
        self.c = self._highlight(order=2, title="c")

    def _orders(self):
        return list(
            Highlight.objects
            .filter(user=self.owner)
            .order_by("order")
            .values_list("title", flat=True)
        )

    def test_reorder_persists_the_new_order(self):
        resp = self._reorder(self.owner, [self.c.id, self.a.id, self.b.id])

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(self._orders(), ["c", "a", "b"])
        self.assertEqual(
            [row["order"] for row in resp.data["data"]["results"]], [0, 1, 2]
        )
        self.assertEqual(self._titles(self._list(self.owner)), ["c", "a", "b"])

    def test_missing_id_is_rejected_without_reordering_anything(self):
        resp = self._reorder(self.owner, [self.c.id, self.a.id])

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("missing", resp.data["message"])
        self.assertEqual(self._orders(), ["a", "b", "c"])

    def test_duplicate_id_is_rejected_without_reordering_anything(self):
        resp = self._reorder(self.owner, [self.a.id, self.a.id, self.b.id, self.c.id])

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self._orders(), ["a", "b", "c"])

    def test_unknown_extra_id_is_rejected_without_reordering_anything(self):
        ghost = self._highlight(user=self.stranger, order=0, title="theirs")
        ghost.delete()

        resp = self._reorder(
            self.owner, [self.c.id, self.b.id, self.a.id, ghost.id]
        )

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self._orders(), ["a", "b", "c"])

    def test_another_users_id_is_rejected_without_reordering_anything(self):
        theirs = self._highlight(user=self.stranger, order=0, title="theirs")

        resp = self._reorder(
            self.owner, [self.c.id, self.b.id, self.a.id, theirs.id]
        )

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("not yours", resp.data["message"])
        self.assertEqual(self._orders(), ["a", "b", "c"])
        theirs.refresh_from_db()
        self.assertEqual(theirs.order, 0)

    def test_deleted_clips_are_not_part_of_the_expected_set(self):
        self._delete(self.owner, self.b.id)

        resp = self._reorder(self.owner, [self.c.id, self.a.id])

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(self._titles(self._list(self.owner)), ["c", "a"])


class HighlightViewCounterTests(HighlightAPITestCase):
    """views_count is denormalized; the owner's own views never count (§2)."""

    def setUp(self):
        super().setUp()
        self.open_clip, self.followers_clip, self.recruiters_clip = self._three_levels()

    def test_scout_view_increments_the_counter(self):
        resp = self._view(self.scout, self.open_clip.id)

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertTrue(resp.data["data"]["counted"])
        self.open_clip.refresh_from_db()
        self.assertEqual(self.open_clip.views_count, 1)

    def test_owner_view_does_not_increment(self):
        resp = self._view(self.owner, self.open_clip.id)

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertFalse(resp.data["data"]["counted"])
        self.open_clip.refresh_from_db()
        self.assertEqual(self.open_clip.views_count, 0)

    def test_concurrent_views_accumulate(self):
        for viewer in (self.scout, self.coach, self.follower):
            self._view(viewer, self.open_clip.id)

        self.open_clip.refresh_from_db()
        self.assertEqual(self.open_clip.views_count, 3)

    def test_viewer_who_cannot_see_the_clip_gets_404_and_no_increment(self):
        resp = self._view(self.stranger, self.recruiters_clip.id)

        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)
        self.recruiters_clip.refresh_from_db()
        self.assertEqual(self.recruiters_clip.views_count, 0)

    def test_view_on_a_deleted_clip_is_404(self):
        self._delete(self.owner, self.open_clip.id)

        resp = self._view(self.scout, self.open_clip.id)

        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_view_requires_authentication(self):
        self.client.force_authenticate(user=None)
        resp = self.client.post(view_url(self.open_clip.id), {}, format="json")

        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)


class HighlightSoftDeleteTests(HighlightAPITestCase):
    """Deleted clips vanish from every list but keep their row (§1)."""

    def test_deleted_clip_disappears_for_every_viewer(self):
        clip = self._highlight(visibility=Highlight.Visibility.EVERYONE, order=0)

        self._delete(self.owner, clip.id)

        for viewer in (self.owner, self.follower, self.stranger, self.scout):
            with self.subTest(viewer=viewer.username):
                self.assertEqual(self._list(viewer).data["data"]["results"], [])

        clip.refresh_from_db()
        self.assertTrue(clip.is_deleted)

    def test_deleting_frees_a_slot_against_the_cap(self):
        for i in range(10):
            self._create(self.owner, {"public_id": f"goatza/highlights/{i}"})

        self.assertEqual(
            self._create(self.owner).status_code, status.HTTP_400_BAD_REQUEST
        )

        oldest = Highlight.objects.filter(user=self.owner).order_by("order").first()
        self.assertEqual(
            self._delete(self.owner, oldest.id).status_code, status.HTTP_200_OK
        )

        resp = self._create(self.owner, {"public_id": "goatza/highlights/replacement"})

        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(
            Highlight.objects.filter(user=self.owner, is_deleted=False).count(), 10
        )

    def test_deleting_twice_is_404(self):
        clip = self._highlight(order=0)
        self._delete(self.owner, clip.id)

        self.assertEqual(
            self._delete(self.owner, clip.id).status_code, status.HTTP_404_NOT_FOUND
        )


class HighlightListQueryCountTests(HighlightAPITestCase):
    """
    The rail must cost a constant number of queries — the serializer reads only
    Highlight's own columns, so there is nothing to select_related and nothing
    that grows with the number of clips.

    Owner / recruiter: 2 (resolve the username, fetch the clips).
    Plain viewer:      3 (+ the follow EXISTS the visibility rules need).
    """

    def _fill(self, count):
        for i in range(count):
            self._highlight(visibility=Highlight.Visibility.EVERYONE, order=i)

    def test_owner_list_is_two_queries_regardless_of_size(self):
        self._fill(3)
        self._auth(self.owner)
        with self.assertNumQueries(2):
            self.client.get(list_url(self.owner.username))

        Highlight.objects.filter(user=self.owner).update(order=F("order") + 10)
        self._fill(6)
        self._auth(self.owner)
        with self.assertNumQueries(2):
            resp = self.client.get(list_url(self.owner.username))
        self.assertEqual(len(resp.data["data"]["results"]), 9)

    def test_recruiter_list_is_two_queries(self):
        self._fill(5)
        self._auth(self.scout)
        with self.assertNumQueries(2):
            self.client.get(list_url(self.owner.username))

    def test_plain_viewer_list_is_three_queries(self):
        self._fill(5)
        self._auth(self.follower)
        with self.assertNumQueries(3):
            self.client.get(list_url(self.owner.username))


# =====================================================================
# REMOVED: eager video-derivative coverage.
#
# It asserted that creating a clip scheduled a server-side transcode. The
# stored object IS the clip that plays now (encoded client-side before
# upload), so there is no derivative to schedule and nothing to assert.
# =====================================================================

class HighlightViewAnalyticsTests(HighlightAPITestCase):
    """
    HighlightView rows + GET /stats/ — the numbers behind "Seen by N recruiters"
    (HIGHLIGHTS_SPEC.md §2, phase 4).
    """

    def setUp(self):
        super().setUp()
        self.clip = self._highlight(
            visibility=Highlight.Visibility.EVERYONE, order=0, title="open"
        )
        self.other_clip = self._highlight(
            visibility=Highlight.Visibility.EVERYONE, order=1, title="second"
        )

    def _stats(self, actor_user, org=None):
        headers = self._auth(actor_user, org)
        return self.client.get(STATS_URL, **headers)

    # ── row writing + dedup ────────────────────────────────────

    def test_a_view_writes_one_row_stamped_with_the_viewer_kind(self):
        self._view(self.scout, self.clip.id)

        row = HighlightView.objects.get()
        self.assertEqual(row.highlight_id, self.clip.id)
        self.assertEqual(row.viewer_user_id, self.scout.id)
        self.assertIsNone(row.viewer_org_id)
        self.assertTrue(row.is_recruiter)
        self.assertEqual(row.viewed_on, timezone.localdate())

    def test_same_viewer_same_day_is_one_row_but_still_counts_plays(self):
        for _ in range(4):
            self._view(self.scout, self.clip.id)

        self.assertEqual(HighlightView.objects.count(), 1)
        # views_count is every play, not the deduplicated row count.
        self.clip.refresh_from_db()
        self.assertEqual(self.clip.views_count, 4)

    def test_same_viewer_next_day_is_a_new_row(self):
        self._view(self.scout, self.clip.id)
        HighlightView.objects.update(
            viewed_on=timezone.localdate() - timedelta(days=1)
        )

        self._view(self.scout, self.clip.id)

        self.assertEqual(HighlightView.objects.count(), 2)

    def test_org_actor_is_recorded_as_the_org_and_as_a_recruiter(self):
        self._view(self.orguser, self.clip.id, org=self.org)

        row = HighlightView.objects.get()
        self.assertIsNone(row.viewer_user_id)
        self.assertEqual(row.viewer_org_id, self.org.id)
        self.assertTrue(row.is_recruiter)

    def test_non_recruiter_viewer_is_recorded_but_not_as_a_recruiter(self):
        self._view(self.follower, self.clip.id)

        row = HighlightView.objects.get()
        self.assertEqual(row.viewer_user_id, self.follower.id)
        self.assertFalse(row.is_recruiter)

    def test_owner_watching_writes_nothing(self):
        self._view(self.owner, self.clip.id)

        self.assertEqual(HighlightView.objects.count(), 0)
        self.clip.refresh_from_db()
        self.assertEqual(self.clip.views_count, 0)

    # ── stats endpoint ─────────────────────────────────────────

    def test_stats_report_plays_and_distinct_recruiters(self):
        # scout watches clip 1 twice (one recruiter), coach watches it once,
        # the org watches clip 2, a plain follower watches clip 1.
        self._view(self.scout, self.clip.id)
        self._view(self.scout, self.clip.id)
        self._view(self.coach, self.clip.id)
        self._view(self.follower, self.clip.id)
        self._view(self.orguser, self.other_clip.id, org=self.org)

        resp = self._stats(self.owner)

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.data["data"]

        by_title = {row["title"]: row for row in data["results"]}
        # 4 plays on clip 1; 2 distinct recruiters (scout + coach, not the follower).
        self.assertEqual(by_title["open"]["views_count"], 4)
        self.assertEqual(by_title["open"]["recruiter_viewers"], 2)
        self.assertEqual(by_title["second"]["views_count"], 1)
        self.assertEqual(by_title["second"]["recruiter_viewers"], 1)

        self.assertEqual(data["totals"]["highlights"], 2)
        self.assertEqual(data["totals"]["views"], 5)
        # scout + coach + org = 3 distinct recruiters across the reel.
        self.assertEqual(data["totals"]["recruiter_viewers"], 3)

    def test_reel_total_does_not_double_count_one_recruiter(self):
        self._view(self.scout, self.clip.id)
        self._view(self.scout, self.other_clip.id)

        data = self._stats(self.owner).data["data"]

        self.assertEqual(data["results"][0]["recruiter_viewers"], 1)
        self.assertEqual(data["results"][1]["recruiter_viewers"], 1)
        # One recruiter watching two clips is still ONE recruiter.
        self.assertEqual(data["totals"]["recruiter_viewers"], 1)

    def test_stats_start_at_zero(self):
        data = self._stats(self.owner).data["data"]

        self.assertEqual(data["totals"]["views"], 0)
        self.assertEqual(data["totals"]["recruiter_viewers"], 0)
        self.assertTrue(all(r["views_count"] == 0 for r in data["results"]))

    def test_deleted_clips_drop_out_of_stats(self):
        self._view(self.scout, self.clip.id)
        self._delete(self.owner, self.clip.id)

        data = self._stats(self.owner).data["data"]

        self.assertEqual(data["totals"]["highlights"], 1)
        self.assertEqual([r["title"] for r in data["results"]], ["second"])

    # ── access ─────────────────────────────────────────────────

    def test_non_players_are_refused(self):
        for viewer in (self.scout, self.coach, self.orguser):
            with self.subTest(viewer=viewer.username):
                self.assertEqual(
                    self._stats(viewer).status_code,
                    status.HTTP_403_FORBIDDEN,
                )

    def test_another_player_gets_their_own_empty_stats_not_the_owners(self):
        """
        The endpoint is self-scoped: there is no parameter to ask for someone
        else's numbers, so a stranger sees their own (empty) reel, never these.
        """
        self._view(self.scout, self.clip.id)

        data = self._stats(self.stranger).data["data"]

        self.assertEqual(data["results"], [])
        self.assertEqual(data["totals"]["views"], 0)
        self.assertEqual(data["totals"]["recruiter_viewers"], 0)

    def test_org_actor_cannot_read_stats(self):
        resp = self._stats(self.orguser, org=self.org)

        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_stats_require_authentication(self):
        self.client.force_authenticate(user=None)

        self.assertEqual(
            self.client.get(STATS_URL).status_code,
            status.HTTP_401_UNAUTHORIZED,
        )


class HighlightUrlShapeTests(APITestCase):
    """
    Guards the URL shape itself, because this is the one class of bug the rest of
    this file CANNOT catch.

    In production the client calls /api/<path> and Vercel rewrites it to the
    backend, stripping /api. A route declared WITH a trailing slash makes Vercel
    308 to the slash-less form first; Django then APPEND_SLASHes back with a
    path-only Location, the /api prefix is lost, and the call lands on the
    frontend's 404 page. Locally none of that happens — the test client and the
    dev server accept the slashed form happily — so a trailing slash sails
    through CI and only breaks in production.
    """

    def test_no_highlight_route_ends_with_a_slash(self):
        from django.urls import get_resolver

        resolver = get_resolver()
        offenders = []

        def walk(patterns, prefix=""):
            for entry in patterns:
                pattern = prefix + str(entry.pattern)
                if hasattr(entry, "url_patterns"):
                    walk(entry.url_patterns, pattern)
                elif pattern.startswith("highlights/") and pattern.endswith("/"):
                    offenders.append(pattern)

        walk(resolver.url_patterns)

        self.assertEqual(
            offenders,
            [],
            "These highlights routes end with a trailing slash and will 404 in "
            f"production behind the /api rewrite: {offenders}",
        )
