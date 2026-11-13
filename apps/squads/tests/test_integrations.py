"""Squads where they meet the rest of the app: career verify, the career card,
account deletion, notifications, log redaction."""

from datetime import date
from unittest.mock import patch

from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.accounts.services.account_deletion_service import confirm_account_deletion
from apps.careers.models import CareerEntry
from apps.careers.selectors.career_selectors import career_entries_for
from apps.careers.serializers.career_serializers import CareerEntrySerializer
from apps.notifications.models import Notification
from apps.notifications.services.deeplink_service import build_notification_url
from apps.notifications.services.grouping_service import NotificationGroupingService
from apps.organization.models import OrganizationMember
from apps.squads.models import Squad, SquadMember
from apps.squads.services.membership_service import MembershipService
from apps.squads.tests.base import SquadTestBase, make_user
from core.log_redaction import redact_token_paths

Status = SquadMember.Status
Verification = CareerEntry.VerificationStatus
T = Notification.Type


class VerifyIntoSquadTests(SquadTestBase):

    def setUp(self):
        super().setUp()
        self.claim = CareerEntry.objects.create(
            user=self.p1, organization=self.org, sport=self.football,
            title="Winger", start_date=date(2025, 1, 1), is_current=True,
            verification_status=Verification.PENDING,
        )

    def verify(self, user, body=None):
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(
                f"/careers/{self.claim.id}/verify", body or {}, format="json",
                **self._org_headers(user),
            )

    def assert_untouched(self):
        self.claim.refresh_from_db()
        self.assertEqual(self.claim.verification_status, Verification.PENDING)
        self.assertFalse(SquadMember.objects.filter(user=self.p1).exists())

    def test_verify_and_add_to_squad(self):
        response = self.verify(self.owner, {
            "squad_id": str(self.squad.id), "position_id": str(self.striker.id),
            "jersey_number": 11,
        })

        self.assertEqual(response.status_code, 200, response.data)
        data = response.data["data"]
        self.assertEqual(data["verification_status"], "verified")
        self.assertEqual(data["squad_member"]["status"], "active")
        self.assertEqual(data["squad_member"]["jersey_number"], 11)
        self.assertEqual(data["squad"]["name"], "U17 Boys")

        row = SquadMember.objects.get(user=self.p1)
        self.assertEqual(row.source, SquadMember.Source.VERIFICATION)
        self.assertEqual(row.career_entry, self.claim)
        self.assertFalse(row.career_entry_created)
        self.assertEqual(row.career_entry_prior_status, "pending")
        self.squad.refresh_from_db()
        self.assertEqual(self.squad.members_count, 1)

        # "Added by mistake" then puts the claim back in the queue.
        MembershipService.remove(self.owner_actor(), self.squad.id, row.id, "mistake")
        self.claim.refresh_from_db()
        self.assertEqual(self.claim.verification_status, Verification.PENDING)

    def test_a_plain_verify_is_unchanged(self):
        response = self.verify(self.owner)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertNotIn("squad_member", response.data["data"])
        self.assertFalse(SquadMember.objects.exists())
        self.assertEqual(
            list(Notification.objects.filter(recipient_user=self.p1).values_list("type", flat=True)),
            [T.CAREER_VERIFIED],
        )

    def test_verify_into_squad_tells_the_player_once_as_a_squad_place(self):
        response = self.verify(self.owner, {"squad_id": str(self.squad.id)})
        self.assertEqual(response.status_code, 200, response.data)

        notifications = Notification.objects.filter(recipient_user=self.p1)
        self.assertEqual(
            list(notifications.values_list("type", flat=True)), [T.SQUAD_REQUEST_APPROVED]
        )
        approved = notifications.get()
        self.assertEqual(approved.actor_org, self.org)
        self.assertEqual(approved.data["squad"]["name"], "U17 Boys")
        pushed = [call.args[1][0] for call in self.push.call_args_list]
        self.assertIn(str(approved.id), pushed)

    def test_a_failed_squad_notification_never_fails_the_verify(self):
        with patch(
            "apps.careers.views.career_verification_views.NotificationService"
            ".squad_request_approved",
            side_effect=RuntimeError("push down"),
        ), self.assertLogs("django.test", "ERROR"):
            response = self.verify(self.owner, {"squad_id": str(self.squad.id)})

        self.assertEqual(response.status_code, 200, response.data)
        self.claim.refresh_from_db()
        self.assertEqual(self.claim.verification_status, Verification.VERIFIED)
        self.assertTrue(SquadMember.objects.filter(user=self.p1, status=Status.ACTIVE).exists())

    def test_a_sport_mismatch_rolls_the_verify_back(self):
        cricket = self.make_squad("Cricket XI", sport=self.cricket)
        self.assert_error(self.verify(self.owner, {"squad_id": str(cricket.id)}), "sport_mismatch")
        self.assert_untouched()

    def test_any_squad_refusal_rolls_the_verify_back(self):
        self.activate(self.squad, self.p2, jersey_number=11)
        self.assert_error(
            self.verify(self.owner, {"squad_id": str(self.squad.id), "jersey_number": 11}),
            "jersey_taken",
        )
        self.assert_untouched()

        other = self.make_squad("Gone")
        Squad.objects.filter(id=other.id).update(is_deleted=True)
        self.assert_error(self.verify(self.owner, {"squad_id": str(other.id)}), "not_found", 404)
        self.assert_untouched()

    def test_a_coach_without_verifications_review(self):
        response = self.verify(self.coach, {"squad_id": str(self.squad.id)})
        self.assertEqual(response.status_code, 403, response.data)
        self.assert_untouched()

    def test_an_owner_without_squads_review(self):
        OrganizationMember.objects.filter(id=self.owner_member.id).update(
            permission_overrides={"revoke": ["squads.review"]}
        )
        self.assert_error(
            self.verify(self.owner, {"squad_id": str(self.squad.id)}), "forbidden", 403
        )
        self.assert_untouched()


class CareerSquadFieldTests(SquadTestBase):

    def _join(self, user, squad, **item):
        row, _ = MembershipService.request_join(user, squad)
        MembershipService.approve(
            self.owner_actor(), squad.id, [{"member_id": str(row.id), **item}]
        )

    def test_shown_for_a_public_squad_hidden_otherwise(self):
        self._join(self.p1, self.squad, jersey_number=7, position_id=str(self.gk.id))

        self.client.force_authenticate(user=self.p2)
        response = self.client.get(f"/careers/users/{self.p1.id}")
        self.assertEqual(response.status_code, 200, response.data)
        entry = response.data["data"]["results"][0]
        self.assertEqual(
            entry["squad"],
            {"id": str(self.squad.id), "name": "U17 Boys", "jersey_number": 7,
             "is_captain": False, "position_name": "Goalkeeper"},
        )

        Squad.objects.filter(id=self.squad.id).update(is_public=False)
        entry = CareerEntrySerializer(career_entries_for(self.p1).first()).data
        self.assertIsNone(entry["squad"])

    def test_no_query_per_entry(self):
        def count():
            with CaptureQueriesContext(connection) as queries:
                CareerEntrySerializer(career_entries_for(self.p1), many=True).data
            return len(queries)

        self._join(self.p1, self.squad)
        before = count()

        rival_squad = self.make_squad("Seniors", sport=self.cricket)
        self._join(self.p1, rival_squad)
        CareerEntry.objects.create(
            user=self.p1, organization_name="Old Club", sport=self.football,
            title="Player", start_date=date(2020, 1, 1),
        )

        self.assertEqual(CareerEntry.objects.filter(user=self.p1).count(), 3)
        self.assertEqual(count(), before)


class AccountDeletionTests(SquadTestBase):

    def test_deletion_ends_memberships_and_recounts(self):
        row, _ = MembershipService.request_join(self.p1, self.squad)
        MembershipService.approve(self.owner_actor(), self.squad.id, [{"member_id": str(row.id)}])
        seniors = self.make_squad("Seniors")
        MembershipService.request_join(self.p1, seniors)

        self.run_after_commit(confirm_account_deletion, self.p1, password="pass1234")

        row.refresh_from_db()
        self.assertEqual(row.status, Status.LEFT)
        self.assertFalse(CareerEntry.objects.get(id=row.career_entry_id).is_current)
        for squad in (self.squad, seniors):
            squad.refresh_from_db()
            self.assertEqual((squad.members_count, squad.pending_count), (0, 0))

    def test_a_failure_there_never_breaks_the_deletion(self):
        with patch.object(
            MembershipService, "end_all_for_user", side_effect=RuntimeError("down")
        ), self.assertLogs("apps.accounts.services.account_deletion_service", "WARNING"):
            self.run_after_commit(confirm_account_deletion, self.p1, password="pass1234")

        self.p1.refresh_from_db()
        self.assertFalse(self.p1.is_active)


class NotificationTests(SquadTestBase):

    def _pushed(self, notification_type):
        ids = [call.args[1][0] for call in self.push.call_args_list]
        return list(
            Notification.objects.filter(id__in=ids, type=notification_type)
            .values_list("recipient_user_id", flat=True)
        )

    def _rows(self, user, kind):
        return Notification.objects.filter(recipient_user=user, type=kind)

    def test_a_join_request_goes_to_the_reviewers(self):
        self.user_post(self.p1, f"invite/{self.squad.invite_token}/join")

        row = self._rows(self.owner, T.SQUAD_JOIN_REQUEST).get()
        self.assertEqual(row.actor_org, self.org)
        self.assertEqual(row.group_key, f"squad_request:{self.squad.id}")
        self.assertEqual(row.data["player_name"], "Asha Player")
        self.assertEqual(
            build_notification_url(row),
            f"/organization/admin/{self.org.id}/squads/{self.squad.id}/requests",
        )
        self.assertTrue(self._rows(self.coach, T.SQUAD_JOIN_REQUEST).exists())
        self.assertFalse(self._rows(self.staff, T.SQUAD_JOIN_REQUEST).exists())

    def test_twenty_requests_are_one_push_per_reviewer_until_read(self):
        players = [make_user(f"fan{index}", f"Fan {index}") for index in range(20)]
        for player in players:
            self.user_post(player, f"invite/{self.squad.invite_token}/join")

        self.assertEqual(self._rows(self.owner, T.SQUAD_JOIN_REQUEST).count(), 20)
        self.assertCountEqual(
            self._pushed(T.SQUAD_JOIN_REQUEST), [self.owner.id, self.coach.id]
        )

        groups = NotificationGroupingService.group_notifications(
            list(self._rows(self.owner, T.SQUAD_JOIN_REQUEST))
        )
        self.assertEqual(len(groups), 1)
        # Twenty different players under one club actor: counted from `data`.
        self.assertRegex(groups[0]["text"], r"^Fan \d+ and 19 others asked to join U17 Boys$")
        self.assertEqual(groups[0]["others_count"], 19)

        self._rows(self.owner, T.SQUAD_JOIN_REQUEST).update(is_read=True)
        self.user_post(self.p1, f"invite/{self.squad.invite_token}/join")
        self.assertEqual(self._pushed(T.SQUAD_JOIN_REQUEST).count(self.owner.id), 2)
        self.assertEqual(self._pushed(T.SQUAD_JOIN_REQUEST).count(self.coach.id), 1)

    def test_the_rest_of_the_lifecycle(self):
        response = self.org_post(
            self.coach, f"{self.squad.id}/invites", {"user_id": str(self.p1.id)}
        )
        invite = self._rows(self.p1, T.SQUAD_INVITE).get()
        self.assertEqual(invite.dedup_key, f"squad_invite:{response.data['data']['id']}")
        self.assertEqual(build_notification_url(invite), "/notifications")
        self.assertIn(self.p1.id, self._pushed(T.SQUAD_INVITE))

        self.user_post(self.p1, f"mine/{response.data['data']['id']}/accept")
        accepted = self._rows(self.coach, T.SQUAD_INVITE_ACCEPTED).get()
        self.assertEqual(
            build_notification_url(accepted),
            f"/organization/admin/{self.org.id}/squads/{self.squad.id}",
        )
        self.assertEqual(self._pushed(T.SQUAD_INVITE_ACCEPTED), [])

        join = self.user_post(self.p2, f"invite/{self.squad.invite_token}/join")
        self.org_post(self.coach, f"{self.squad.id}/requests/approve", {
            "items": [{"member_id": join.data["data"]["member_id"]}],
        })
        approved = self._rows(self.p2, T.SQUAD_REQUEST_APPROVED).get()
        self.assertEqual(
            build_notification_url(approved),
            f"/organization/profile/dreamfc/squads/{self.squad.id}",
        )
        self.assertIn(self.p2.id, self._pushed(T.SQUAD_REQUEST_APPROVED))

        join = self.user_post(self.p3, f"invite/{self.squad.invite_token}/join")
        self.org_post(self.coach, f"{self.squad.id}/requests/decline", {
            "member_ids": [join.data["data"]["member_id"]],
        })
        self.assertTrue(self._rows(self.p3, T.SQUAD_REQUEST_DECLINED).exists())
        self.assertEqual(self._pushed(T.SQUAD_REQUEST_DECLINED), [])

        seniors = self.make_squad("Seniors")
        member_id = response.data["data"]["id"]
        self.org_post(self.coach, f"{self.squad.id}/members/{member_id}/move", {
            "to_squad_id": str(seniors.id),
        })
        moved = self._rows(self.p1, T.SQUAD_MEMBER_MOVED).get()
        self.assertEqual(moved.data["to_squad"]["name"], "Seniors")
        self.assertEqual(
            build_notification_url(moved), f"/organization/profile/dreamfc/squads/{seniors.id}"
        )

        member_id = approved.data["member_id"]
        self.org_post(self.coach, f"{self.squad.id}/members/{member_id}/remove", {"reason": "left"})
        removed = self._rows(self.p2, T.SQUAD_MEMBER_REMOVED).get()
        self.assertEqual(removed.data["squad"]["name"], "U17 Boys")
        self.assertEqual(self._pushed(T.SQUAD_MEMBER_REMOVED), [])

        for row in Notification.objects.filter(type__startswith="squad_"):
            self.assertEqual(row.actor_org, self.org)
            self.assertNotIn("birthdate", row.data)
            self.assertNotIn("notes", row.data)

    def test_a_failed_notification_never_fails_the_write(self):
        with patch(
            "apps.notifications.services.notification_service.NotificationService"
            ".squad_join_request",
            side_effect=RuntimeError("push down"),
        ), self.assertLogs("django.test", "ERROR"):
            # robust=True: the failure is logged and the commit stands.
            response = self.user_post(self.p1, f"invite/{self.squad.invite_token}/join")

        self.assertEqual(response.status_code, 201, response.data)
        self.assertTrue(SquadMember.objects.filter(user=self.p1, status=Status.PENDING).exists())


class LogRedactionTests(SquadTestBase):

    def test_squad_invite_paths(self):
        self.assertEqual(
            redact_token_paths('"POST /squads/invite/AbC_123-x/join HTTP/1.1"'),
            '"POST /squads/invite/[redacted]/join HTTP/1.1"',
        )
        self.assertEqual(
            redact_token_paths("https://goatza.com/squad-invite/AbC_123-x"),
            "https://goatza.com/squad-invite/[redacted]",
        )
        # The club's own routes carry no token.
        path = f"/squads/{self.squad.id}/invites"
        self.assertEqual(redact_token_paths(path), path)
