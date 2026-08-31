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
    OrganizationSport,
)
from connections.models import Follow
from posts.models import Post, Like
from sports.models import Sport, UserSport, SportPosition, UserSportPosition

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
        accept_current_terms(user)
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
        accept_current_terms(self.me)
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
        accept_current_terms(flat)
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
        accept_current_terms(flat)
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
        accept_current_terms(flat)
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
        accept_current_terms(flat)
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
        accept_current_terms(user)
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


PLAYER_KEYS = {
    "id", "name", "username", "role", "headline", "profile_photo",
    "city", "followers_count", "distance_km", "is_following",
    # Batched with the page (highlights.selectors.visible_highlight_counts_for)
    # to drive the "▶ Highlights (n)" chip on the card.
    "highlights_count",
}
ORG_KEYS = {
    "id", "name", "username", "type", "is_verified", "logo", "headline",
    "level", "city", "followers_count", "distance_km", "is_following",
}


class ExplorePlayersFilterTests(APITestCase):
    """GET /feed/explore/players — search / sport / position / location filters."""

    def setUp(self):
        # Actor anchored at BASE with a location.
        self.me = self._player("me", "Me Myself")
        self.sport = Sport.objects.create(name="Football", icon_name="mdi:soccer")
        self.other_sport = Sport.objects.create(name="Cricket", icon_name="mdi:cricket")
        self.striker = SportPosition.objects.create(sport=self.sport, name="Striker")
        self.keeper = SportPosition.objects.create(sport=self.sport, name="Keeper")
        self.batsman = SportPosition.objects.create(
            sport=self.other_sport, name="Batsman"
        )

    # ── factories ────────────────────────────────────────────────

    def _player(self, username, name, lat=BASE_LAT, lng=BASE_LNG, followers=0):
        user = User.objects.create_user(
            email=f"{username}@example.com",
            password="pass1234",
            username=username,
            role=User.Role.PLAYER,
        )
        accept_current_terms(user)
        UserProfile.objects.create(
            user=user,
            name=name,
            headline=f"{name} headline",
            city=f"{username}-city",
            latitude=lat,
            longitude=lng,
            followers_count=followers,
        )
        return user

    def _add_sport(self, user, sport):
        return UserSport.objects.create(user=user, sport=sport)

    def _add_position(self, user, sport, position):
        return UserSportPosition.objects.create(
            user=user, sport=sport, position=position
        )

    def _get(self, actor_user=None, **params):
        self.client.force_authenticate(user=actor_user or self.me)
        return self.client.get(PLAYERS_URL, params)

    def _ids(self, resp):
        return [str(r["id"]) for r in resp.data["data"]["results"]]

    def _row(self, resp, obj):
        return next(
            (r for r in resp.data["data"]["results"] if str(r["id"]) == str(obj.id)),
            None,
        )

    # ── search ───────────────────────────────────────────────────

    def test_search_matches_name_and_username_case_insensitive(self):
        alice = self._player("alice", "Alice Wonder")
        bob = self._player("bobby", "Bob Builder")

        self.assertEqual(self._ids(self._get(search="alice")), [str(alice.id)])
        self.assertEqual(self._ids(self._get(search="WONDER")), [str(alice.id)])
        self.assertEqual(self._ids(self._get(search="bobby")), [str(bob.id)])

    # ── sport / position ─────────────────────────────────────────

    def test_sport_filter_via_usersport(self):
        footballer = self._player("footballer", "Foot Baller")
        self._add_sport(footballer, self.sport)
        cricketer = self._player("cricketer", "Crick Eter")
        self._add_sport(cricketer, self.other_sport)
        nosport = self._player("nosport", "No Sport")

        ids = self._ids(self._get(sport_id=str(self.sport.id)))
        self.assertIn(str(footballer.id), ids)
        self.assertNotIn(str(cricketer.id), ids)
        self.assertNotIn(str(nosport.id), ids)

    def test_position_happy_path(self):
        striker_player = self._player("strikerp", "Striker Player")
        self._add_sport(striker_player, self.sport)
        self._add_position(striker_player, self.sport, self.striker)
        keeper_player = self._player("keeperp", "Keeper Player")
        self._add_sport(keeper_player, self.sport)
        self._add_position(keeper_player, self.sport, self.keeper)

        ids = self._ids(
            self._get(sport_id=str(self.sport.id), position_id=str(self.striker.id))
        )
        self.assertIn(str(striker_player.id), ids)
        self.assertNotIn(str(keeper_player.id), ids)

    def test_position_from_wrong_sport_returns_400(self):
        resp = self._get(
            sport_id=str(self.sport.id), position_id=str(self.batsman.id)
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("does not belong", resp.data["message"])

    def test_position_without_sport_returns_400(self):
        resp = self._get(position_id=str(self.striker.id))
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("sport_id", resp.data["message"])

    def test_invalid_sport_id_returns_400(self):
        resp = self._get(sport_id="not-a-uuid")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_unknown_sport_id_returns_400(self):
        import uuid as _uuid

        resp = self._get(sport_id=str(_uuid.uuid4()))
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("not found", resp.data["message"].lower())

    # ── location ─────────────────────────────────────────────────

    def test_location_filter_inside_outside_and_ordered(self):
        near = self._player("near", "Near", lat=11.0, lng=76.0)   # ~0 km
        mid = self._player("mid", "Mid", lat=11.3, lng=76.0)      # ~33 km
        far = self._player("far", "Far", lat=11.6, lng=76.0)      # ~66 km

        resp = self._get(lat=11.0, lng=76.0, radius_km=50)
        self.assertEqual(resp.data["data"]["mode"], "nearby")

        ids = self._ids(resp)
        self.assertEqual(ids, [str(near.id), str(mid.id)])
        self.assertNotIn(str(far.id), ids)

        dists = [r["distance_km"] for r in resp.data["data"]["results"]]
        self.assertEqual(dists, sorted(dists))

    def test_location_default_radius_is_50(self):
        inside = self._player("inside", "Inside", lat=11.3, lng=76.0)    # ~33 km
        outside = self._player("outside", "Outside", lat=11.6, lng=76.0)  # ~66 km

        ids = self._ids(self._get(lat=11.0, lng=76.0))  # no radius → 50
        self.assertIn(str(inside.id), ids)
        self.assertNotIn(str(outside.id), ids)

    def test_radius_clamped_to_max_500(self):
        near = self._player("near", "Near", lat=11.0, lng=76.0)           # ~0 km
        veryfar = self._player("veryfar", "Very Far", lat=20.0, lng=76.0)  # ~999 km

        ids = self._ids(self._get(lat=11.0, lng=76.0, radius_km=100000))
        self.assertIn(str(near.id), ids)
        self.assertNotIn(str(veryfar.id), ids)  # 999 km > clamped 500

    def test_lat_without_lng_returns_400(self):
        resp = self._get(lat=11.0)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("together", resp.data["message"])

    def test_lat_out_of_range_returns_400(self):
        resp = self._get(lat=120, lng=76)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    # ── mode rules ───────────────────────────────────────────────

    def test_search_forces_popular_mode_globally(self):
        # Far outside the actor's nearby radius, found only because search is global.
        farstar = self._player("farstar", "Far Star", lat=40.0, lng=76.0)

        resp = self._get(search="far star")
        self.assertEqual(resp.data["data"]["mode"], "popular")
        self.assertIn(str(farstar.id), self._ids(resp))

    def test_location_filter_forces_nearby_and_never_falls_back(self):
        # A popular player exists, but nobody is inside the filter radius.
        self._player("pop", "Pop One", lat=11.0, lng=76.0, followers=99)

        resp = self._get(lat=40.0, lng=76.0, radius_km=50)  # empty area
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["data"]["mode"], "nearby")  # NOT popular
        self.assertEqual(self._ids(resp), [])

    # ── followed inclusion + is_following ────────────────────────

    def test_followed_hidden_in_rails_visible_when_searching(self):
        zoe = self._player("zoe", "Zoe Zebra")
        Follow.objects.create(follower_user=self.me, following_user=zoe)

        # rails (no params) → followed hidden
        self.assertNotIn(str(zoe.id), self._ids(self._get()))

        # searching → followed visible, flagged is_following
        resp = self._get(search="zoe")
        row = self._row(resp, zoe)
        self.assertIsNotNone(row)
        self.assertTrue(row["is_following"])

    # ── no-param regression guard ────────────────────────────────

    def test_no_params_shape_and_behaviour_unchanged(self):
        p1 = self._player("p1", "P One")
        followed = self._player("fol", "Fol Lowed")
        Follow.objects.create(follower_user=self.me, following_user=followed)

        resp = self._get()
        data = resp.data["data"]

        self.assertEqual(set(data.keys()), {"next_cursor", "mode", "results"})
        self.assertEqual(data["mode"], "nearby")  # actor has a location

        ids = [str(r["id"]) for r in data["results"]]
        self.assertIn(str(p1.id), ids)
        self.assertNotIn(str(followed.id), ids)  # rails still exclude followed

        self.assertEqual(set(data["results"][0].keys()), PLAYER_KEYS)
        self.assertFalse(any(r["is_following"] for r in data["results"]))

    # ── pagination with a filter ─────────────────────────────────

    def test_pagination_with_search_no_dupes(self):
        created = [
            self._player(f"k{i}", f"Kicker {i}", followers=i) for i in range(12)
        ]

        seen, cursor, pages = [], None, 0
        while True:
            params = {"search": "kicker"}
            if cursor:
                params["cursor"] = cursor
            resp = self._get(**params)
            self.assertEqual(resp.data["data"]["mode"], "popular")
            page = self._ids(resp)
            self.assertLessEqual(len(page), 10)
            seen.extend(page)
            cursor = resp.data["data"]["next_cursor"]
            pages += 1
            if not cursor or pages > 5:
                break

        self.assertEqual(len(seen), 12)
        self.assertEqual(set(seen), {str(p.id) for p in created})


class ExploreOrganizationsFilterTests(APITestCase):
    """GET /feed/explore/organizations — search / sport / location filters."""

    def setUp(self):
        self.me = User.objects.create_user(
            email="me@example.com", password="pass1234", username="me"
        )
        accept_current_terms(self.me)
        UserProfile.objects.create(
            user=self.me, name="Me", latitude=BASE_LAT, longitude=BASE_LNG
        )
        self.sport = Sport.objects.create(name="Football", icon_name="mdi:soccer")
        self.other_sport = Sport.objects.create(name="Cricket", icon_name="mdi:cricket")

    # ── factories ────────────────────────────────────────────────

    def _org(self, username, name, type=Organization.Type.CLUB,
             followers=0, lat=BASE_LAT, lng=BASE_LNG):
        org = Organization.objects.create(name=name, username=username, type=type)
        OrganizationProfile.objects.create(
            organization=org,
            logo=f"https://cdn.example.com/{username}.png",
            followers_count=followers,
        )
        if lat is not None and lng is not None:
            OrganizationLocation.objects.create(
                organization=org, city=f"{username}-city", country_code="IN",
                latitude=lat, longitude=lng, is_primary=True,
            )
        return org

    def _add_sport(self, org, sport):
        return OrganizationSport.objects.create(organization=org, sport=sport)

    def _get(self, actor_user=None, **params):
        self.client.force_authenticate(user=actor_user or self.me)
        return self.client.get(ORGS_URL, params)

    def _ids(self, resp):
        return [str(r["id"]) for r in resp.data["data"]["results"]]

    def _row(self, resp, obj):
        return next(
            (r for r in resp.data["data"]["results"] if str(r["id"]) == str(obj.id)),
            None,
        )

    # ── search ───────────────────────────────────────────────────

    def test_search_matches_name_and_username(self):
        united = self._org("unitedfc", "United FC")
        rovers = self._org("roversclub", "Rovers Club")

        self.assertEqual(self._ids(self._get(search="united")), [str(united.id)])
        self.assertEqual(self._ids(self._get(search="ROVERSCLUB")), [str(rovers.id)])

    # ── sport ────────────────────────────────────────────────────

    def test_sport_filter_via_org_sport(self):
        footy = self._org("footy", "Footy FC")
        self._add_sport(footy, self.sport)
        cricky = self._org("cricky", "Cricky CC")
        self._add_sport(cricky, self.other_sport)

        ids = self._ids(self._get(sport_id=str(self.sport.id)))
        self.assertIn(str(footy.id), ids)
        self.assertNotIn(str(cricky.id), ids)

    # ── location ─────────────────────────────────────────────────

    def test_location_filter_inside_outside(self):
        near = self._org("near", "Near FC", lat=11.0, lng=76.0)   # ~0 km
        far = self._org("far", "Far FC", lat=11.6, lng=76.0)      # ~66 km

        resp = self._get(lat=11.0, lng=76.0, radius_km=50)
        self.assertEqual(resp.data["data"]["mode"], "nearby")
        ids = self._ids(resp)
        self.assertIn(str(near.id), ids)
        self.assertNotIn(str(far.id), ids)

    def test_lat_without_lng_returns_400(self):
        resp = self._get(lat=11.0)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_position_id_rejected_for_orgs(self):
        import uuid as _uuid

        resp = self._get(
            sport_id=str(self.sport.id), position_id=str(_uuid.uuid4())
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("not supported", resp.data["message"])

    # ── followed inclusion + is_following ────────────────────────

    def test_followed_hidden_visible_with_search_and_is_following(self):
        acme = self._org("acme", "Acme United")
        Follow.objects.create(follower_user=self.me, following_org=acme)

        self.assertNotIn(str(acme.id), self._ids(self._get()))

        resp = self._get(search="acme")
        row = self._row(resp, acme)
        self.assertIsNotNone(row)
        self.assertTrue(row["is_following"])

    # ── no-param regression guard ────────────────────────────────

    def test_no_params_shape_unchanged(self):
        near = self._org("nearfc", "Near FC", lat=11.0, lng=76.0)
        followed = self._org("folfc", "Fol FC", lat=11.0, lng=76.0)
        Follow.objects.create(follower_user=self.me, following_org=followed)

        resp = self._get()
        data = resp.data["data"]

        self.assertEqual(set(data.keys()), {"next_cursor", "mode", "results"})
        ids = [str(r["id"]) for r in data["results"]]
        self.assertIn(str(near.id), ids)
        self.assertNotIn(str(followed.id), ids)   # rails still exclude followed
        self.assertEqual(set(data["results"][0].keys()), ORG_KEYS)


# ══════════════════════════════════════════════════════════════════
# HOME FEED — ranked serving (Feed Ranking spec, Phase 1)
# ══════════════════════════════════════════════════════════════════

import base64
import uuid as uuid_module
from unittest.mock import patch

from django.core.cache import cache

from feed.models import ActorAffinity, PostImpression
from feed.services.feed_services import MAX_POSTS_PER_AUTHOR_PER_PAGE, PAGE_SIZE
from feed.services.ranking_services import FeedRankingService
from legal.testing import accept_current_terms

FEED_URL = "/feed/list"
IMPRESSIONS_URL = "/feed/impressions"


class FeedTestBase(APITestCase):
    """Shared factories + helpers for the ranked home feed."""

    def setUp(self):
        # The ranking is cached per (actor, hour bucket); LocMem survives
        # between tests in one process, so start every test from empty.
        cache.clear()
        self.me = self._user("me", "Me")

    # ── factories ────────────────────────────────────────────────

    def _user(self, username, name):
        user = User.objects.create_user(
            email=f"{username}@example.com",
            password="pass1234",
            username=username,
        )
        accept_current_terms(user)
        UserProfile.objects.create(
            user=user, name=name,
            profile_photo=f"https://cdn.example.com/{username}.jpg",
        )
        return user

    def _org(self, username, name):
        org = Organization.objects.create(
            name=name, username=username, type=Organization.Type.CLUB
        )
        OrganizationProfile.objects.create(organization=org)
        return org

    def _follow(self, target):
        if isinstance(target, User):
            Follow.objects.create(follower_user=self.me, following_user=target)
        else:
            Follow.objects.create(follower_user=self.me, following_org=target)
        return target

    def _followed_user(self, username, name=None):
        return self._follow(self._user(username, name or username.title()))

    def _post(self, author, likes=0, comments=0, age_hours=1,
              visibility=Post.Visibility.PUBLIC, content="post", sport=None):
        kwargs = dict(
            content=content, visibility=visibility,
            likes_count=likes, comments_count=comments, sport=sport,
        )
        if isinstance(author, User):
            kwargs["author_user"] = author
        else:
            kwargs["author_org"] = author
        post = Post.objects.create(**kwargs)
        # created_at is auto_now_add — a raw UPDATE is the only way to age it.
        Post.objects.filter(pk=post.pk).update(
            created_at=timezone.now() - timedelta(hours=age_hours)
        )
        post.refresh_from_db()
        return post

    def _impression(self, post, hours_ago):
        return PostImpression.objects.create(
            user=self.me,
            post=post,
            last_seen_at=timezone.now() - timedelta(hours=hours_ago),
        )

    # ── request helpers ──────────────────────────────────────────

    def _get(self, **params):
        self.client.force_authenticate(user=self.me)
        return self.client.get(FEED_URL, params)

    def _ids(self, resp):
        return [str(r["id"]) for r in resp.data["data"]["results"]]

    def _no_jitter(self):
        """
        Pin the §3.5 noise to 1.0 for tests that assert a SCORE ordering.

        The jitter band is ±10%, so it can legitimately swap two posts whose
        scores are within ~22% of each other — and ln() keeps engagement
        differences well inside that. Leaving it on would make these tests
        assert the dice roll rather than the rule they are named after. The
        jitter itself is covered by FeedJitterTests.
        """
        return patch.object(
            FeedRankingService, "_jitter", staticmethod(lambda post_id, seed: 1.0)
        )


class FeedDecayScoringTests(FeedTestBase):
    """§3.1 — multiplicative gravity replaces the additive recency boost."""

    def test_old_popular_post_ranks_below_fresh_quiet_post(self):
        """
        The exact inversion §1 names as the root cause: under the old formula
        2*ln(20 interactions) beat the maximum freshness bonus, so the post
        stayed pinned to the top forever. Under decay it must not.
        """
        loud = self._followed_user("loud")
        quiet = self._followed_user("quiet")

        old_popular = self._post(loud, likes=20, age_hours=72)   # 3 days
        fresh_quiet = self._post(quiet, likes=0, age_hours=0)

        with self._no_jitter():
            ids = self._ids(self._get())
        self.assertLess(
            ids.index(str(fresh_quiet.id)),
            ids.index(str(old_popular.id)),
        )

    def test_engagement_still_wins_between_equally_fresh_posts(self):
        """Decay demotes age, not engagement — same age, more likes, higher."""
        a = self._followed_user("aaa")
        b = self._followed_user("bbb")

        popular = self._post(a, likes=40, age_hours=2)
        unpopular = self._post(b, likes=0, age_hours=2)

        with self._no_jitter():
            ids = self._ids(self._get())
        self.assertLess(ids.index(str(popular.id)), ids.index(str(unpopular.id)))

    def test_comments_outweigh_likes(self):
        a = self._followed_user("ca")
        b = self._followed_user("cb")

        commented = self._post(a, likes=0, comments=10, age_hours=2)
        liked = self._post(b, likes=10, comments=0, age_hours=2)

        with self._no_jitter():
            ids = self._ids(self._get())
        self.assertLess(ids.index(str(commented.id)), ids.index(str(liked.id)))


class FeedSeenPenaltyTests(FeedTestBase):
    """§3.2 — persistent impressions push read posts down, never out."""

    def test_recently_seen_post_drops_below_an_unseen_one(self):
        a = self._followed_user("seenauthor")
        b = self._followed_user("unseenauthor")

        # `winner` outranks `loser` on score alone …
        winner = self._post(a, likes=30, age_hours=2)
        loser = self._post(b, likes=10, age_hours=2)

        with self._no_jitter():
            ids = self._ids(self._get())
        self.assertLess(ids.index(str(winner.id)), ids.index(str(loser.id)))

        # … until it is marked read an hour ago (x0.2), which flips the pair.
        cache.clear()
        self._impression(winner, hours_ago=1)

        with self._no_jitter():
            ids = self._ids(self._get())
        self.assertLess(ids.index(str(loser.id)), ids.index(str(winner.id)))

    def test_seen_posts_are_penalised_not_excluded(self):
        author = self._followed_user("onlyauthor")
        post = self._post(author, likes=5, age_hours=2)
        self._impression(post, hours_ago=1)

        # With a thin pool, exclusion would empty the feed entirely.
        self.assertIn(str(post.id), self._ids(self._get()))

    def test_penalty_softens_as_the_impression_ages(self):
        a = self._followed_user("recentseen")
        b = self._followed_user("staleseen")

        recent = self._post(a, likes=10, age_hours=2)
        stale = self._post(b, likes=10, age_hours=2)

        self._impression(recent, hours_ago=1)        # x0.2
        self._impression(stale, hours_ago=24 * 5)    # x0.8

        with self._no_jitter():
            ids = self._ids(self._get())
        self.assertLess(ids.index(str(stale.id)), ids.index(str(recent.id)))

    def test_impressions_are_per_person_not_per_actor(self):
        """
        Reading a post as yourself must silence it after switching to your club:
        the same human already read it.
        """
        org = self._org("myclub", "My Club")
        OrganizationMember.objects.create(
            organization=org, user=self.me,
            role=OrganizationMember.Role.OWNER,
        )
        author = self._user("stranger", "Stranger")
        Follow.objects.create(follower_org=org, following_user=author)

        winner = self._post(author, likes=30, age_hours=2)
        loser = self._post(author, likes=10, age_hours=2)
        self._impression(winner, hours_ago=1)

        self.client.force_authenticate(user=self.me)
        with self._no_jitter():
            resp = self.client.get(
                FEED_URL,
                HTTP_X_ACTOR_TYPE="organization",
                HTTP_X_ACTOR_ID=str(org.id),
            )
        ids = self._ids(resp)
        self.assertLess(ids.index(str(loser.id)), ids.index(str(winner.id)))


class FeedAuthorCapTests(FeedTestBase):
    """§3.3 — at most two posts per author per page, and nothing dropped."""

    def test_cap_pushes_posts_forward_instead_of_dropping_them(self):
        heavy = self._followed_user("heavy")
        heavy_posts = [
            self._post(heavy, likes=50 + i, age_hours=2) for i in range(5)
        ]
        other_posts = [
            self._post(self._followed_user(f"other{i}"), likes=5, age_hours=2)
            for i in range(5)
        ]
        heavy_ids = {str(p.id) for p in heavy_posts}

        collected, cursor, pages = [], None, 0
        while True:
            params = {"cursor": cursor} if cursor else {}
            resp = self._get(**params)
            page_ids = self._ids(resp)

            heavy_on_page = sum(1 for pid in page_ids if pid in heavy_ids)
            self.assertLessEqual(heavy_on_page, MAX_POSTS_PER_AUTHOR_PER_PAGE)
            self.assertLessEqual(len(page_ids), PAGE_SIZE)

            collected.extend(page_ids)
            cursor = resp.data["data"]["next_cursor"]
            pages += 1
            if not cursor or pages > 8:
                break

        expected = {str(p.id) for p in heavy_posts + other_posts}
        self.assertEqual(len(collected), len(set(collected)))   # no duplicates
        self.assertEqual(set(collected), expected)              # nothing dropped

    def test_a_user_and_their_org_count_as_different_authors(self):
        org = self._org("clubfc", "Club FC")
        self._follow(org)
        person = self._followed_user("person")

        org_posts = [self._post(org, likes=20, age_hours=2) for _ in range(2)]
        person_posts = [self._post(person, likes=20, age_hours=2) for _ in range(2)]

        # 4 posts, 2 authors, cap 2 each → one page holds all of them.
        ids = self._ids(self._get())
        self.assertEqual(
            set(ids), {str(p.id) for p in org_posts + person_posts}
        )


class FeedPaginationTests(FeedTestBase):
    """Session-ranked pagination: no duplicates, no gaps, cache-miss safe."""

    def _many_posts(self, count=40, authors=8):
        people = [self._followed_user(f"a{i}") for i in range(authors)]
        return [
            self._post(people[i % authors], likes=i, age_hours=1 + i)
            for i in range(count)
        ]

    def test_pages_have_no_duplicates_and_no_gaps(self):
        posts = self._many_posts()

        collected, cursor, pages = [], None, 0
        while True:
            resp = self._get(**({"cursor": cursor} if cursor else {}))
            self.assertEqual(resp.status_code, status.HTTP_200_OK)
            collected.extend(self._ids(resp))
            cursor = resp.data["data"]["next_cursor"]
            pages += 1
            if not cursor or pages > 8:
                break

        self.assertEqual(len(collected), len(set(collected)))
        self.assertEqual(set(collected), {str(p.id) for p in posts})

    def test_first_three_pages_are_disjoint(self):
        # 60 posts over 20 authors → four full pages, so a cursor still exists
        # after the third and the walk is genuinely mid-feed.
        self._many_posts(count=60, authors=20)

        seen = set()
        cursor = None
        for _ in range(3):
            resp = self._get(**({"cursor": cursor} if cursor else {}))
            page = self._ids(resp)
            self.assertEqual(len(page), PAGE_SIZE)
            self.assertTrue(seen.isdisjoint(page))
            seen.update(page)
            cursor = resp.data["data"]["next_cursor"]
            self.assertIsNotNone(cursor)

    def test_cache_miss_between_pages_serves_no_duplicates(self):
        """
        A redeploy (or the 10-minute TTL) drops the cached ranking mid-scroll.
        The seed rides in the cursor so the rebuild comes out near-identical,
        and seen_ids covers the residual drift — which is why it stays.
        """
        self._many_posts(count=45, authors=15)

        first = self._get()
        page_one = self._ids(first)
        cursor = first.data["data"]["next_cursor"]

        cache.clear()   # ← the ranking is gone

        second = self._get(cursor=cursor, seen_ids=",".join(page_one))
        page_two = self._ids(second)

        self.assertTrue(set(page_one).isdisjoint(page_two))

    def test_cursor_ends_the_feed_once_the_ranking_is_exhausted(self):
        self._many_posts(count=20, authors=10)

        cursor, pages = None, 0
        while True:
            resp = self._get(**({"cursor": cursor} if cursor else {}))
            cursor = resp.data["data"]["next_cursor"]
            pages += 1
            if not cursor:
                break
            self.assertLess(pages, 6)

        self.assertIsNone(cursor)

    def test_invalid_cursor_returns_400(self):
        resp = self._get(cursor="garbage")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_forged_cursor_seed_is_rejected(self):
        # The seed is concatenated into a cache key, so it is validated like
        # any other untrusted input.
        forged = base64.b64encode(b"bad seed\n|0").decode()
        resp = self._get(cursor=forged)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)


class FeedJitterTests(FeedTestBase):
    """§3.5 — session-seeded exploration noise."""

    def _ranking(self, seed):
        actor = type("StubActor", (), {
            "is_user": True, "is_org": False,
            "user": self.me, "organization": None,
        })()
        return FeedRankingService._build_ranking(actor, self.me, seed)

    def test_same_seed_produces_the_same_ordering(self):
        for i in range(12):
            self._post(self._followed_user(f"j{i}"), likes=10, age_hours=2)

        first = self._ranking("user_x:1")
        second = self._ranking("user_x:1")

        self.assertEqual(first["pages"], second["pages"])

    def test_different_seed_produces_a_different_ordering(self):
        for i in range(12):
            self._post(self._followed_user(f"k{i}"), likes=10, age_hours=2)

        first = self._ranking("user_x:1")
        second = self._ranking("user_x:2")

        self.assertNotEqual(first["pages"], second["pages"])
        # Same posts, only reordered — jitter must not add or drop anything.
        self.assertEqual(
            sorted(sum(first["pages"], [])),
            sorted(sum(second["pages"], [])),
        )

    def test_jitter_factor_stays_inside_the_configured_band(self):
        factors = [
            FeedRankingService._jitter(f"post-{i}", "seed-1") for i in range(200)
        ]
        self.assertTrue(all(0.9 <= f < 1.1 for f in factors))
        # …and actually varies, or it would not be jitter.
        self.assertGreater(max(factors) - min(factors), 0.1)


class FeedBlendingTests(FeedTestBase):
    """§3.4 — three candidate sources, de-duplicated, always backfilled."""

    def test_feed_is_not_empty_without_any_followed_accounts(self):
        # The backfill path: source 1 is empty, so the whole page has to come
        # from trending / interest.
        posts = [
            self._post(self._user(f"nobody{i}", "Nobody"), likes=5, age_hours=2)
            for i in range(3)
        ]

        ids = self._ids(self._get())
        self.assertEqual(set(ids), {str(p.id) for p in posts})

    def test_followed_posts_are_labelled_followed(self):
        author = self._followed_user("friend")
        self._post(author, likes=5, age_hours=2)

        row = self._get().data["data"]["results"][0]
        self.assertEqual(row["feed_source"], "followed")

    def test_stranger_posts_are_labelled_for_the_suggested_chip(self):
        stranger = self._user("astranger", "A Stranger")
        self._post(stranger, likes=30, age_hours=2)

        row = self._get().data["data"]["results"][0]
        self.assertIn(row["feed_source"], {"trending", "interest"})

    def test_sport_interest_posts_surface_from_non_followed_authors(self):
        sport = Sport.objects.create(name="Football", icon_name="mdi:soccer")
        UserSport.objects.create(user=self.me, sport=sport, is_primary=True)

        stranger = self._user("baller", "Baller")
        tagged = self._post(stranger, likes=0, age_hours=2, sport=sport)

        ids = self._ids(self._get())
        self.assertIn(str(tagged.id), ids)

    def test_a_post_is_never_served_twice_across_sources(self):
        # A followed author's post also qualifies as trending.
        author = self._followed_user("dual")
        post = self._post(author, likes=99, age_hours=1)

        ids = self._ids(self._get())
        self.assertEqual(ids.count(str(post.id)), 1)

    def test_soft_deleted_and_invisible_posts_never_appear(self):
        stranger = self._user("hidden", "Hidden")
        deleted = self._post(stranger, likes=5)
        Post.objects.filter(pk=deleted.pk).update(is_deleted=True)
        followers_only = self._post(
            stranger, likes=5, visibility=Post.Visibility.FOLLOWERS
        )
        visible = self._post(stranger, likes=5)

        ids = self._ids(self._get())
        self.assertIn(str(visible.id), ids)
        self.assertNotIn(str(deleted.id), ids)
        self.assertNotIn(str(followers_only.id), ids)


class FeedImpressionEndpointTests(FeedTestBase):
    """POST /feed/impressions — fire-and-forget telemetry."""

    def _flush(self, payload):
        self.client.force_authenticate(user=self.me)
        return self.client.post(IMPRESSIONS_URL, payload, format="json")

    def test_stores_impressions_and_returns_204(self):
        author = self._user("w", "W")
        posts = [self._post(author) for _ in range(3)]

        resp = self._flush({"post_ids": [str(p.id) for p in posts]})

        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(PostImpression.objects.filter(user=self.me).count(), 3)

    def test_caps_at_one_hundred_ids(self):
        author = self._user("w", "W")
        posts = Post.objects.bulk_create([
            Post(author_user=author, content=f"p{i}") for i in range(150)
        ])

        resp = self._flush({"post_ids": [str(p.id) for p in posts]})

        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(PostImpression.objects.filter(user=self.me).count(), 100)

    def test_repeat_flush_increments_seen_count(self):
        author = self._user("w", "W")
        post = self._post(author)
        payload = {"post_ids": [str(post.id)]}

        self._flush(payload)
        self._flush(payload)
        self._flush(payload)

        impression = PostImpression.objects.get(user=self.me, post=post)
        self.assertEqual(impression.seen_count, 3)

    def test_malformed_and_unknown_ids_are_ignored_silently(self):
        author = self._user("w", "W")
        good = self._post(author)

        resp = self._flush({
            "post_ids": [
                str(good.id),
                "not-a-uuid",
                str(uuid_module.uuid4()),   # well-formed, but no such post
                None,
            ]
        })

        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)
        stored = list(
            PostImpression.objects.filter(user=self.me)
            .values_list("post_id", flat=True)
        )
        self.assertEqual(stored, [good.id])

    def test_missing_or_junk_body_still_returns_204(self):
        self.assertEqual(
            self._flush({}).status_code, status.HTTP_204_NO_CONTENT
        )
        self.assertEqual(
            self._flush({"post_ids": "nope"}).status_code,
            status.HTTP_204_NO_CONTENT,
        )

    def test_recorded_impressions_change_the_next_feed(self):
        a = self._followed_user("ia")
        b = self._followed_user("ib")
        winner = self._post(a, likes=30, age_hours=2)
        loser = self._post(b, likes=10, age_hours=2)

        self._flush({"post_ids": [str(winner.id)]})
        cache.clear()

        with self._no_jitter():
            ids = self._ids(self._get())
        self.assertLess(ids.index(str(loser.id)), ids.index(str(winner.id)))


class FeedAffinityTests(FeedTestBase):
    """§3.6 — incremental affinity with decay applied at both ends."""

    LIKE_URL = "/posts/like"

    def test_liking_a_post_creates_and_then_grows_affinity(self):
        author = self._user("fav", "Fav")
        first = self._post(author)
        second = self._post(author)

        self.client.force_authenticate(user=self.me)
        self.client.post(self.LIKE_URL, {"post_id": str(first.id)}, format="json")
        self.client.post(self.LIKE_URL, {"post_id": str(second.id)}, format="json")

        affinity = ActorAffinity.objects.get(viewer=self.me, author_user=author)
        # Two likes at +2, decayed by ~nothing over a few milliseconds.
        self.assertAlmostEqual(affinity.score, 4.0, places=2)

    def test_affinity_lifts_a_favourite_author(self):
        favourite = self._followed_user("favourite")
        other = self._followed_user("other")

        lifted = self._post(favourite, likes=10, age_hours=2)
        rival = self._post(other, likes=12, age_hours=2)

        with self._no_jitter():
            ids = self._ids(self._get())
        self.assertLess(ids.index(str(rival.id)), ids.index(str(lifted.id)))

        ActorAffinity.objects.create(
            viewer=self.me, author_user=favourite, score=4.0
        )
        cache.clear()

        with self._no_jitter():
            ids = self._ids(self._get())
        self.assertLess(ids.index(str(lifted.id)), ids.index(str(rival.id)))

    def test_affinity_contribution_is_capped(self):
        from feed.selectors.feed_selectors import affinities_for
        from feed.services.feed_services import AFFINITY_CAP

        author = self._user("huge", "Huge")
        ActorAffinity.objects.create(
            viewer=self.me, author_user=author, score=500.0
        )

        self.assertEqual(affinities_for(self.me)[str(author.id)], AFFINITY_CAP)

    def test_self_interaction_records_nothing(self):
        mine = self._post(self.me)

        self.client.force_authenticate(user=self.me)
        self.client.post(self.LIKE_URL, {"post_id": str(mine.id)}, format="json")

        self.assertFalse(ActorAffinity.objects.filter(viewer=self.me).exists())
