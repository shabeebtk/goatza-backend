"""Featured players: opt-in, verified career only, the squads privacy rules."""

import datetime

from django.core.cache import cache

from apps.accounts.models import UserProfile
from apps.careers.models import CareerEntry
from apps.orgpages.models import FeaturedPlayer
from apps.orgpages.tests.base import MEDIA, PageTestBase, child_birthdate, make_user


class PlayerEditorTests(PageTestBase):

    def test_only_verified_career_candidates(self):
        self.verified_entry(self.p1)
        self.verified_entry(self.p2, verification_status=CareerEntry.VerificationStatus.PENDING)
        data = self.org_get(self.owner, "players").data["data"]

        self.assertEqual(data["players"], [])
        self.assertEqual([c["user"]["username"] for c in data["candidates"]], ["asha"])
        candidate = data["candidates"][0]
        self.assertEqual(candidate["years_label"], "2019 – 2023")
        self.assertTrue(candidate["can_feature"])

        self.assert_error(
            self.org_post(self.owner, "players", {"user_id": str(self.p2.id)}), "not_eligible"
        )

    def test_a_private_profile_cannot_be_featured(self):
        self.verified_entry(self.p1)
        UserProfile.objects.filter(user=self.p1).update(is_public_profile=False)

        candidate = self.org_get(self.owner, "players").data["data"]["candidates"][0]
        self.assertFalse(candidate["is_public_profile"])
        self.assertFalse(candidate["can_feature"])
        self.assert_error(
            self.org_post(self.owner, "players", {"user_id": str(self.p1.id)}), "not_eligible"
        )

    def test_pick_edit_reorder_remove(self):
        self.verified_entry(self.p1)
        self.verified_entry(self.p2, current=True, start=datetime.date(2021, 1, 1))

        response = self.org_post(self.owner, "players", {
            "user_id": str(self.p1.id), "highlight": "State U19 squad",
        })
        self.assertEqual(response.status_code, 201, response.data)
        self.org_post(self.owner, "players", {"user_id": str(self.p2.id)})
        self.assert_error(
            self.org_post(self.owner, "players", {"user_id": str(self.p2.id)}), "already_added"
        )

        players = self.org_get(self.owner, "players").data["data"]["players"]
        self.assertEqual([p["user"]["username"] for p in players], ["asha", "bilal"])
        self.assertEqual(players[1]["years_label"], "2021 – now")

        ids = [p["id"] for p in players]
        response = self.org_post(self.owner, "players/reorder", {"ids": ids[::-1]})
        self.assertEqual(
            [p["user"]["username"] for p in response.data["data"]["players"]], ["bilal", "asha"]
        )

        response = self.org_patch(self.owner, f"players/{ids[0]}", {"highlight": "x" * 61})
        self.assert_error(response, "invalid", field="highlight")

        self.org_post(self.owner, f"players/{ids[0]}/remove")
        self.assertEqual(FeaturedPlayer.objects.filter(organization=self.org).count(), 1)

    def test_the_account_purge_removes_their_rows(self):
        from apps.accounts.management.commands.purge_deleted_accounts import Command

        entry = self.verified_entry(self.p1)
        FeaturedPlayer.objects.create(organization=self.org, user=self.p1, career_entry=entry)
        with self.captureOnCommitCallbacks(execute=True):
            Command()._purge(self.p1)
        self.assertFalse(FeaturedPlayer.objects.filter(user=self.p1).exists())


class PlayerPublicTests(PageTestBase):

    def feature(self, user, **fields):
        entry = fields.pop("entry", None) or self.verified_entry(user)
        return FeaturedPlayer.objects.create(
            organization=self.org, user=user, career_entry=entry, **fields
        )

    def players(self, viewer=None):
        cache.clear()
        if viewer is None:
            return self.public_page().data["data"]["players"]
        return self.showcase(viewer).data["data"]["players"]

    def test_nothing_shows_until_picked(self):
        self.verified_entry(self.p1)
        self.verified_entry(self.p2)
        self.assertEqual(self.players(), [])

        self.feature(self.p3)
        self.assertEqual([p["username"] for p in self.players()], ["chen"])

    def test_anonymous_hides_a_player_who_went_private_and_strips_a_minors_photo(self):
        self.feature(self.p1)
        self.assertEqual(len(self.players()), 1)
        UserProfile.objects.filter(user=self.p1).update(is_public_profile=False)
        self.assertEqual(self.players(), [])
        # Signed in, a private profile still shows.
        self.assertEqual(len(self.players(viewer=self.p2)), 1)

        kid = make_user("kiddo", "Kid Player", birthdate=child_birthdate())
        self.feature(kid)
        anonymous = {p["username"]: p for p in self.players()}
        self.assertEqual(anonymous["kiddo"]["photo"], "")
        signed_in = {p["username"]: p for p in self.players(viewer=self.p2)}
        self.assertEqual(signed_in["kiddo"]["photo"], f"{MEDIA}/users/kiddo.webp")

    def test_a_deactivated_account_or_unverified_entry_never_shows(self):
        row = self.feature(self.p1)
        self.feature(self.p2)
        type(self.p2).objects.filter(id=self.p2.id).update(is_active=False)
        CareerEntry.objects.filter(id=row.career_entry_id).update(
            verification_status=CareerEntry.VerificationStatus.PENDING
        )
        self.assertEqual(self.players(), [])
        self.assertEqual(self.players(viewer=self.p3), [])

    def test_a_blocked_viewer_does_not_see_them(self):
        self.feature(self.p1)
        self.feature(self.p2)
        self.block(self.p1, target_user=self.p3)
        self.assertEqual([p["username"] for p in self.players(viewer=self.p3)], ["bilal"])

    def test_now_at_and_highlight(self):
        self.feature(self.p1)
        city = self.make_org("City FC", "cityfc")
        older = self.make_org("Old Town", "oldtown")
        self.verified_entry(self.p1, org=older, current=True, start=datetime.date(2022, 1, 1))
        self.verified_entry(self.p1, org=city, current=True, start=datetime.date(2024, 1, 1))
        # Not current, or not verified: never "now at".
        self.verified_entry(self.p1, org=self.make_org("Gone", "gone"), start=datetime.date(2025, 1, 1),
                            end=datetime.date(2025, 6, 1))
        self.verified_entry(self.p1, org=self.make_org("Pending", "pendingfc"), current=True,
                            start=datetime.date(2025, 2, 1),
                            verification_status=CareerEntry.VerificationStatus.PENDING)

        player = self.players()[0]
        self.assertEqual(player["now_at"], "City FC")
        self.assertIsNone(player["highlight"])

        FeaturedPlayer.objects.filter(user=self.p1).update(highlight="State U19 squad")
        player = self.players()[0]
        self.assertEqual(player["highlight"], "State U19 squad")
        self.assertIsNone(player["now_at"])
