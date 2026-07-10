from datetime import timedelta

from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import User, UserProfile
from organization.models import (
    Organization,
    OrganizationProfile,
    OrganizationLocation,
    OrganizationMember,
)
from connections.models import Follow
from posts.models import Post, Like

PLAYERS_URL = "/feed/explore/players"
ORGS_URL = "/feed/explore/organizations"
POSTS_URL = "/feed/explore/posts"

# Actor anchor point (Kozhikode-ish). ~111 km per degree of latitude.
BASE_LAT = 11.0
BASE_LNG = 76.0


class ExplorePlayersTests(APITestCase):
    """GET /feed/explore/players — nearby / popular discovery."""

    def setUp(self):
        # The acting user, anchored at BASE with a location set.
        self.me = self._player("me", "Me Myself", lat=BASE_LAT, lng=BASE_LNG)

    # ── factories ────────────────────────────────────────────────

    def _player(self, username, name, lat=None, lng=None, followers=0,
                role=User.Role.PLAYER, with_profile=True):
        user = User.objects.create_user(
            email=f"{username}@example.com",
            password="pass1234",
            username=username,
            role=role,
        )
        if with_profile:
            UserProfile.objects.create(
                user=user,
                name=name,
                headline=f"{name} headline",
                city=f"{username}-city",
                profile_photo=f"https://cdn.example.com/{username}.jpg",
                latitude=lat,
                longitude=lng,
                followers_count=followers,
            )
        return user

    def _org(self, username, name):
        org = Organization.objects.create(
            name=name, username=username, type=Organization.Type.CLUB
        )
        OrganizationProfile.objects.create(organization=org)
        return org

    # ── request helpers ──────────────────────────────────────────

    def _get(self, actor_user=None, org=None, **params):
        self.client.force_authenticate(user=actor_user or self.me)
        headers = {}
        if org is not None:
            headers = {
                "HTTP_X_ACTOR_TYPE": "organization",
                "HTTP_X_ACTOR_ID": str(org.id),
            }
        return self.client.get(PLAYERS_URL, params, **headers)

    def _ids(self, resp):
        return [str(r["id"]) for r in resp.data["data"]["results"]]

    # ── nearby mode ──────────────────────────────────────────────

    def test_nearby_orders_by_distance_and_excludes_out_of_radius(self):
        near = self._player("near", "Near", lat=11.0, lng=76.0)      # ~0 km
        mid = self._player("mid", "Mid", lat=11.5, lng=76.0)         # ~55 km
        edge = self._player("edge", "Edge", lat=12.0, lng=76.0)      # ~111 km
        far = self._player("far", "Far", lat=13.5, lng=76.0)         # ~278 km (out)

        resp = self._get()
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["data"]["mode"], "nearby")

        ids = self._ids(resp)
        # ordered nearest-first, far one dropped by the bounding box.
        self.assertEqual(ids, [str(near.id), str(mid.id), str(edge.id)])
        self.assertNotIn(str(far.id), ids)

        # distance_km present and ascending in nearby mode.
        distances = [r["distance_km"] for r in resp.data["data"]["results"]]
        self.assertTrue(all(d is not None for d in distances))
        self.assertEqual(distances, sorted(distances))
        self.assertAlmostEqual(distances[0], 0.0, delta=1.0)

    def test_excludes_self_followed_nonplayers_and_profileless(self):
        followed = self._player("followed", "Followed", lat=11.0, lng=76.0)
        Follow.objects.create(follower_user=self.me, following_user=followed)

        coach = self._player("coach", "Coach", lat=11.0, lng=76.0,
                             role=User.Role.COACH)
        scout = self._player("scout", "Scout", lat=11.0, lng=76.0,
                             role=User.Role.SCOUT)
        no_profile = self._player("noprof", "NoProf", with_profile=False)
        keeper = self._player("keeper", "Keeper", lat=11.0, lng=76.0)

        ids = self._ids(self._get())

        self.assertIn(str(keeper.id), ids)
        self.assertNotIn(str(self.me.id), ids)          # self
        self.assertNotIn(str(followed.id), ids)         # already following
        self.assertNotIn(str(coach.id), ids)            # not a player
        self.assertNotIn(str(scout.id), ids)            # not a player
        self.assertNotIn(str(no_profile.id), ids)       # no profile

    # ── popular mode ─────────────────────────────────────────────

    def test_no_location_actor_switches_to_popular(self):
        # actor has a profile but no coordinates → popular.
        flat = self._player("flat", "Flat", lat=None, lng=None)
        self.client.force_authenticate(user=flat)

        p_lo = self._player("plo", "Lo", lat=11.0, lng=76.0, followers=1)
        p_hi = self._player("phi", "Hi", lat=11.0, lng=76.0, followers=99)
        p_mid = self._player("pmid", "Mid", lat=11.0, lng=76.0, followers=50)

        resp = self.client.get(PLAYERS_URL)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["data"]["mode"], "popular")

        ids = self._ids(resp)
        # most-followed first; the "me" anchor (0 followers) sits at the tail.
        self.assertEqual(ids[:3], [str(p_hi.id), str(p_mid.id), str(p_lo.id)])
        # distance_km is null in popular mode.
        self.assertTrue(
            all(r["distance_km"] is None for r in resp.data["data"]["results"])
        )

    def test_nearby_empty_first_page_falls_back_to_popular(self):
        # actor HAS a location, but every other player is out of radius →
        # the first nearby page is empty → auto-switch to popular.
        far1 = self._player("far1", "Far1", lat=40.0, lng=76.0, followers=5)
        far2 = self._player("far2", "Far2", lat=41.0, lng=76.0, followers=9)

        resp = self._get()
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["data"]["mode"], "popular")
        ids = self._ids(resp)
        self.assertEqual(ids[:2], [str(far2.id), str(far1.id)])

    # ── cursor pagination ────────────────────────────────────────

    def test_pagination_stable_mode_no_duplicates_same_distance(self):
        # 25 players at the EXACT same spot → identical distance, so the id
        # tie-break is what keeps the keyset total-ordered across pages.
        created = [
            self._player(f"p{i}", f"P{i}", lat=11.0, lng=76.0)
            for i in range(25)
        ]

        seen = []
        cursor = None
        pages = 0
        while True:
            params = {"cursor": cursor} if cursor else {}
            resp = self._get(**params)
            self.assertEqual(resp.status_code, status.HTTP_200_OK)
            self.assertEqual(resp.data["data"]["mode"], "nearby")  # never flips

            page_ids = self._ids(resp)
            self.assertLessEqual(len(page_ids), 10)  # page size
            seen.extend(page_ids)

            cursor = resp.data["data"]["next_cursor"]
            pages += 1
            if not cursor:
                break
            self.assertLess(pages, 10)  # guard against runaway loops

        # exactly the 25 created players, each once (no dupes, none skipped).
        self.assertEqual(len(seen), 25)
        self.assertEqual(set(seen), {str(u.id) for u in created})

    def test_pagination_popular_no_duplicates(self):
        flat = self._player("flat", "Flat", lat=None, lng=None)
        self.client.force_authenticate(user=flat)
        created = [
            self._player(f"q{i}", f"Q{i}", followers=i) for i in range(15)
        ]

        seen, cursor, pages = [], None, 0
        while True:
            params = {"cursor": cursor} if cursor else {}
            resp = self.client.get(PLAYERS_URL, params)
            self.assertEqual(resp.data["data"]["mode"], "popular")
            seen.extend(self._ids(resp))
            cursor = resp.data["data"]["next_cursor"]
            pages += 1
            if not cursor or pages > 10:
                break

        # 15 created + the flat anchor = 16 distinct rows, no duplicates.
        self.assertEqual(len(seen), len(set(seen)))
        self.assertIn(str(created[-1].id), seen)

    def test_invalid_cursor_returns_400(self):
        resp = self._get(cursor="not-a-real-cursor")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    # ── org actor ────────────────────────────────────────────────

    def test_org_actor_discovers_players_excluding_followed(self):
        org = self._org("dreamfc", "Dream FC")
        OrganizationMember.objects.create(
            organization=org, user=self.me, role=OrganizationMember.Role.OWNER
        )

        followed = self._player("ofollowed", "OFollowed", lat=11.0, lng=76.0)
        Follow.objects.create(follower_org=org, following_user=followed)
        visible = self._player("ovisible", "OVisible", lat=11.0, lng=76.0)

        # org has no primary location → popular mode for the org actor.
        resp = self._get(org=org)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        ids = self._ids(resp)

        self.assertIn(str(visible.id), ids)
        self.assertNotIn(str(followed.id), ids)   # org already follows them
        # acting as an org, the logged-in user is NOT excluded from players.
        self.assertIn(str(self.me.id), ids)


class ExploreOrganizationsTests(APITestCase):
    """GET /feed/explore/organizations — nearby / popular + types filter."""

    def setUp(self):
        self.me = User.objects.create_user(
            email="me@example.com", password="pass1234", username="me"
        )
        UserProfile.objects.create(
            user=self.me, name="Me", latitude=BASE_LAT, longitude=BASE_LNG
        )

    # ── factories ────────────────────────────────────────────────

    def _org(self, username, name, type=Organization.Type.CLUB,
             followers=0, lat=None, lng=None, is_active=True, is_primary=True):
        org = Organization.objects.create(
            name=name, username=username, type=type, is_active=is_active
        )
        OrganizationProfile.objects.create(
            organization=org,
            logo=f"https://cdn.example.com/{username}.png",
            headline=f"{name} HQ",
            level=OrganizationProfile.Level.PROFESSIONAL,
            followers_count=followers,
        )
        if lat is not None and lng is not None:
            OrganizationLocation.objects.create(
                organization=org,
                city=f"{username}-city",
                country_code="IN",
                latitude=lat,
                longitude=lng,
                is_primary=is_primary,
            )
        return org

    # ── request helpers ──────────────────────────────────────────

    def _get(self, actor_user=None, org=None, **params):
        self.client.force_authenticate(user=actor_user or self.me)
        headers = {}
        if org is not None:
            headers = {
                "HTTP_X_ACTOR_TYPE": "organization",
                "HTTP_X_ACTOR_ID": str(org.id),
            }
        return self.client.get(ORGS_URL, params, **headers)

    def _ids(self, resp):
        return [str(r["id"]) for r in resp.data["data"]["results"]]

    # ── nearby mode ──────────────────────────────────────────────

    def test_nearby_orders_by_distance_and_drops_out_of_radius(self):
        near = self._org("near", "Near FC", lat=11.0, lng=76.0)     # ~0 km
        mid = self._org("mid", "Mid FC", lat=11.5, lng=76.0)        # ~55 km
        far = self._org("far", "Far FC", lat=13.0, lng=76.0)        # ~222 km (out)

        resp = self._get()
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["data"]["mode"], "nearby")

        ids = self._ids(resp)
        self.assertEqual(ids, [str(near.id), str(mid.id)])
        self.assertNotIn(str(far.id), ids)

        row = resp.data["data"]["results"][0]
        self.assertEqual(row["city"], "near-city")           # from primary loc
        self.assertEqual(row["level"], "professional")       # from profile
        self.assertIsNotNone(row["distance_km"])

    def test_no_duplicate_rows_with_multiple_locations(self):
        # An org with several locations (one primary) must appear exactly once.
        org = self._org("multi", "Multi FC", lat=11.0, lng=76.0)
        OrganizationLocation.objects.create(
            organization=org, city="branch-a", country_code="IN",
            latitude=11.1, longitude=76.0, is_primary=False,
        )
        OrganizationLocation.objects.create(
            organization=org, city="branch-b", country_code="IN",
            latitude=11.2, longitude=76.0, is_primary=False,
        )

        ids = self._ids(self._get())
        self.assertEqual(ids.count(str(org.id)), 1)
        # city resolves from the PRIMARY location, not a branch.
        row = next(r for r in self._get().data["data"]["results"]
                   if str(r["id"]) == str(org.id))
        self.assertEqual(row["city"], "multi-city")

    # ── popular mode ─────────────────────────────────────────────

    def test_no_location_actor_switches_to_popular(self):
        flat = User.objects.create_user(
            email="flat@example.com", password="pass1234", username="flat"
        )
        UserProfile.objects.create(user=flat, name="Flat")  # no coords

        lo = self._org("lo", "Lo FC", followers=2)
        hi = self._org("hi", "Hi FC", followers=88)
        mid = self._org("mid", "Mid FC", followers=40)

        resp = self._get(actor_user=flat)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["data"]["mode"], "popular")

        ids = self._ids(resp)
        self.assertEqual(ids, [str(hi.id), str(mid.id), str(lo.id)])
        self.assertTrue(
            all(r["distance_km"] is None for r in resp.data["data"]["results"])
        )

    # ── types filter ─────────────────────────────────────────────

    def test_types_filter_narrows_results(self):
        flat = User.objects.create_user(
            email="f2@example.com", password="pass1234", username="f2"
        )
        UserProfile.objects.create(user=flat, name="F2")

        club = self._org("club1", "Club One", type=Organization.Type.CLUB)
        team = self._org("team1", "Team One", type=Organization.Type.TEAM)
        academy = self._org("acad1", "Acad One", type=Organization.Type.ACADEMY)

        resp = self._get(actor_user=flat, types="club,team")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        ids = self._ids(resp)
        self.assertIn(str(club.id), ids)
        self.assertIn(str(team.id), ids)
        self.assertNotIn(str(academy.id), ids)

    def test_invalid_type_returns_400(self):
        resp = self._get(types="club,banana")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("banana", resp.data["message"])

    # ── exclusions ───────────────────────────────────────────────

    def test_excludes_inactive_and_followed_orgs(self):
        flat = User.objects.create_user(
            email="f3@example.com", password="pass1234", username="f3"
        )
        UserProfile.objects.create(user=flat, name="F3")

        active = self._org("active", "Active FC", followers=5)
        inactive = self._org("inactive", "Inactive FC", is_active=False)
        followed = self._org("followed", "Followed FC", followers=7)
        Follow.objects.create(follower_user=flat, following_org=followed)

        ids = self._ids(self._get(actor_user=flat))
        self.assertIn(str(active.id), ids)
        self.assertNotIn(str(inactive.id), ids)     # is_active=False
        self.assertNotIn(str(followed.id), ids)     # already following

    def test_org_actor_excludes_own_org(self):
        my_org = self._org("myorg", "My Org", lat=BASE_LAT, lng=BASE_LNG)
        OrganizationMember.objects.create(
            organization=my_org, user=self.me,
            role=OrganizationMember.Role.OWNER,
        )
        other = self._org("other", "Other FC", lat=11.0, lng=76.0)

        ids = self._ids(self._get(org=my_org))
        self.assertIn(str(other.id), ids)
        self.assertNotIn(str(my_org.id), ids)       # never discover yourself

    # ── pagination ───────────────────────────────────────────────

    def test_pagination_stable_and_no_duplicates(self):
        flat = User.objects.create_user(
            email="f4@example.com", password="pass1234", username="f4"
        )
        UserProfile.objects.create(user=flat, name="F4")
        created = [self._org(f"o{i}", f"O{i}", followers=i) for i in range(23)]

        seen, cursor, pages = [], None, 0
        while True:
            params = {"cursor": cursor} if cursor else {}
            resp = self._get(actor_user=flat, **params)
            self.assertEqual(resp.status_code, status.HTTP_200_OK)
            self.assertEqual(resp.data["data"]["mode"], "popular")
            page_ids = self._ids(resp)
            self.assertLessEqual(len(page_ids), 10)
            seen.extend(page_ids)
            cursor = resp.data["data"]["next_cursor"]
            pages += 1
            if not cursor or pages > 10:
                break

        self.assertEqual(len(seen), 23)
        self.assertEqual(set(seen), {str(o.id) for o in created})


class ExploreTrendingPostsTests(APITestCase):
    """GET /feed/explore/posts — engagement-first trending feed."""

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

    def _post(self, author, likes=0, comments=0,
              visibility=Post.Visibility.PUBLIC, is_deleted=False,
              age_hours=1, content="post"):
        kwargs = dict(
            content=content, visibility=visibility,
            likes_count=likes, comments_count=comments, is_deleted=is_deleted,
        )
        if isinstance(author, User):
            kwargs["author_user"] = author
        else:
            kwargs["author_org"] = author
        post = Post.objects.create(**kwargs)
        # created_at is auto_now_add — override via a raw UPDATE to control age.
        Post.objects.filter(pk=post.pk).update(
            created_at=timezone.now() - timedelta(hours=age_hours)
        )
        post.refresh_from_db()
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
        return self.client.get(POSTS_URL, params, **headers)

    def _ids(self, resp):
        return [str(r["id"]) for r in resp.data["data"]["results"]]

    # ── visibility / eligibility ─────────────────────────────────

    def test_only_public_recent_live_posts_appear(self):
        stranger = self._user("s", "S")
        good = self._post(stranger, likes=5)
        deleted = self._post(stranger, likes=5, is_deleted=True)
        followers_only = self._post(
            stranger, likes=5, visibility=Post.Visibility.FOLLOWERS
        )
        old = self._post(stranger, likes=5, age_hours=24 * 40)  # 40 days

        ids = self._ids(self._get())
        self.assertIn(str(good.id), ids)
        self.assertNotIn(str(deleted.id), ids)          # soft-deleted
        self.assertNotIn(str(followers_only.id), ids)   # not public
        self.assertNotIn(str(old.id), ids)              # outside 30-day window

    def test_excludes_own_posts_user_actor(self):
        stranger = self._user("s", "S")
        mine = self._post(self.me, likes=99)
        theirs = self._post(stranger, likes=1)

        ids = self._ids(self._get())
        self.assertNotIn(str(mine.id), ids)
        self.assertIn(str(theirs.id), ids)

    # ── scoring ──────────────────────────────────────────────────

    def test_engagement_first_ordering(self):
        a = self._user("a", "A")
        b = self._user("b", "B")
        c = self._user("c", "C")
        low = self._post(a, likes=1)
        high = self._post(b, likes=100)
        mid = self._post(c, likes=20)

        # distinct authors → diversification preserves the pure score order.
        ids = self._ids(self._get())
        self.assertEqual(ids, [str(high.id), str(mid.id), str(low.id)])

    def test_followed_author_ranks_below_equal_stranger(self):
        followed = self._user("followed", "Followed")
        stranger = self._user("stranger", "Stranger")
        Follow.objects.create(follower_user=self.me, following_user=followed)

        # identical engagement + recency → only the followed penalty separates.
        f_post = self._post(followed, likes=10, comments=2, age_hours=1)
        s_post = self._post(stranger, likes=10, comments=2, age_hours=1)

        ids = self._ids(self._get())
        # both surface (mix), but the stranger ranks above the followed author.
        self.assertIn(str(f_post.id), ids)
        self.assertIn(str(s_post.id), ids)
        self.assertLess(ids.index(str(s_post.id)), ids.index(str(f_post.id)))

    # ── diversification ──────────────────────────────────────────

    def test_diversify_lifts_other_authors(self):
        heavy = self._user("heavy", "Heavy")
        light = self._user("light", "Light")
        # Heavy dominates by score with 3 posts; light has a single low post.
        h_posts = [self._post(heavy, likes=50) for _ in range(3)]
        l_post = self._post(light, likes=1)

        ids = self._ids(self._get())
        self.assertEqual(len(ids), 4)
        # Without diversify the light post would sit last; round-robin lifts it
        # to the 2nd slot so one author can't monopolize the top.
        self.assertEqual(ids[1], str(l_post.id))
        self.assertNotEqual(ids[-1], str(l_post.id))

    # ── seen_ids variety ─────────────────────────────────────────

    def test_seen_ids_excludes_those_posts(self):
        stranger = self._user("s", "S")
        posts = [self._post(stranger, likes=i) for i in range(4)]
        seen = f"{posts[0].id},{posts[1].id}"

        ids = self._ids(self._get(seen_ids=seen))
        self.assertNotIn(str(posts[0].id), ids)
        self.assertNotIn(str(posts[1].id), ids)
        self.assertIn(str(posts[2].id), ids)
        self.assertIn(str(posts[3].id), ids)

    def test_seen_ids_junk_is_ignored_wholesale(self):
        stranger = self._user("s", "S")
        post = self._post(stranger, likes=5)

        # A malformed token voids the whole param (same as FeedAPIView) → the
        # post is NOT excluded.
        ids = self._ids(self._get(seen_ids="not-a-uuid"))
        self.assertIn(str(post.id), ids)

    # ── pagination ───────────────────────────────────────────────

    def test_cursor_multiple_pages_with_seen_ids_no_dupes(self):
        authors = [self._user(f"a{i}", f"A{i}") for i in range(4)]
        posts = [
            self._post(authors[i % 4], likes=i, age_hours=1)
            for i in range(20)
        ]
        seen = [str(posts[0].id), str(posts[1].id)]
        seen_param = ",".join(seen)

        collected, cursor, pages = [], None, 0
        while True:
            params = {"seen_ids": seen_param}
            if cursor:
                params["cursor"] = cursor
            resp = self._get(**params)
            self.assertEqual(resp.status_code, status.HTTP_200_OK)
            page_ids = self._ids(resp)
            self.assertLessEqual(len(page_ids), 15)  # page size
            collected.extend(page_ids)
            cursor = resp.data["data"]["next_cursor"]
            pages += 1
            if not cursor or pages > 6:
                break

        # 20 posts − 2 seen = 18 unique rows, no duplicates, none skipped.
        self.assertEqual(len(collected), 18)
        self.assertEqual(len(collected), len(set(collected)))
        self.assertNotIn(seen[0], collected)
        self.assertNotIn(seen[1], collected)

    def test_invalid_cursor_returns_400(self):
        resp = self._get(cursor="garbage")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    # ── response shape + reactions ───────────────────────────────

    def test_response_shape_and_reaction_context(self):
        stranger = self._user("s", "S")
        post = self._post(stranger, likes=3)
        Like.objects.create(user=self.me, post=post, type=Like.Type.FIRE)

        resp = self._get()
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.data["data"]
        self.assertIn("next_cursor", data)
        self.assertIn("results", data)

        row = next(r for r in data["results"] if str(r["id"]) == str(post.id))
        self.assertTrue(row["reaction"]["is_reacted"])
        self.assertEqual(row["reaction"]["type"], "fire")

    # ── org actor ────────────────────────────────────────────────

    def test_org_actor_excludes_own_and_penalizes_followed(self):
        org = self._org("dreamfc", "Dream FC")
        OrganizationMember.objects.create(
            organization=org, user=self.me, role=OrganizationMember.Role.OWNER
        )
        followed_user = self._user("fu", "FU")
        stranger = self._user("st", "St")
        Follow.objects.create(follower_org=org, following_user=followed_user)

        own = self._post(org, likes=50)                       # org's own post
        f_post = self._post(followed_user, likes=10, age_hours=1)
        s_post = self._post(stranger, likes=10, age_hours=1)  # equal to f_post

        ids = self._ids(self._get(org=org))
        self.assertNotIn(str(own.id), ids)                    # own org post
        # org follows followed_user → their post is deprioritized vs stranger.
        self.assertLess(ids.index(str(s_post.id)), ids.index(str(f_post.id)))
