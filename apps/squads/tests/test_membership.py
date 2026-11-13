"""MembershipService: the three ways in, approving, and THE CAREER ENTRY RULES."""

from datetime import date, timedelta
from unittest.mock import patch

from django.utils import timezone

from apps.accounts.models import User
from apps.careers.models import CareerEntry
from apps.organization.models import Organization, OrganizationMember, OrganizationProfile
from apps.recruitments.models import Recruitment, RecruitmentApplication
from apps.squads.errors import CANNOT_ADD_MESSAGE
from apps.squads.models import Squad, SquadMember
from apps.squads.services.membership_service import MembershipService
from apps.squads.tests.base import SquadTestBase, make_user

Status = SquadMember.Status
Verification = CareerEntry.VerificationStatus


class MembershipTestBase(SquadTestBase):

    def request(self, user, squad=None):
        return self.run_after_commit(MembershipService.request_join, user, squad or self.squad)

    def approve(self, squad, *items, actor=None):
        return self.run_after_commit(
            MembershipService.approve, actor or self.owner_actor(), squad.id, list(items)
        )

    def invite(self, user, squad=None, actor=None, **kwargs):
        return self.run_after_commit(
            MembershipService.invite,
            actor or self.owner_actor(), (squad or self.squad).id, user.id, **kwargs,
        )

    def accept(self, user, row):
        return self.run_after_commit(MembershipService.accept_invite, user, row.id)

    def join_squad(self, user, squad=None, **item):
        """Ask + approve: an ACTIVE row with its career entry, as production makes it."""
        row, _ = self.request(user, squad)
        self.approve(squad or self.squad, {"member_id": str(row.id), **item})
        row.refresh_from_db()
        return row

    def entry(self, user, status=Verification.PENDING, org=None, sport=None, **fields):
        fields.setdefault("is_current", True)
        return CareerEntry.objects.create(
            user=user,
            organization=org or self.org,
            sport=sport or self.football,
            title="Midfielder",
            start_date=date(2025, 1, 1),
            verification_status=status,
            **fields,
        )

    def squad_counts(self, squad=None):
        squad = squad or self.squad
        squad.refresh_from_db()
        return squad.members_count, squad.pending_count


class LinkTests(MembershipTestBase):

    def test_asking_twice_is_one_request(self):
        row, created = self.request(self.p1)
        again, created_again = self.request(self.p1)

        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(row.id, again.id)
        self.assertEqual(row.status, Status.PENDING)
        self.assertEqual(row.source, SquadMember.Source.LINK)
        self.assertEqual(self.squad_counts(), (0, 1))

    def test_request_limit(self):
        with patch("apps.squads.services.membership_service.MAX_PENDING_PER_SQUAD", 1):
            self.request(self.p1)
            self.assert_raises_code("request_limit", MembershipService.request_join, self.p2, self.squad)

    def test_only_players(self):
        for role in (User.Role.COACH, User.Role.SCOUT):
            person = make_user(f"a{role}", role=role)
            self.assert_raises_code("not_a_player", MembershipService.request_join, person, self.squad)

    def test_a_turned_off_link(self):
        Squad.objects.filter(id=self.squad.id).update(invite_enabled=False)
        self.squad.refresh_from_db()
        self.assert_raises_code("invite_closed", MembershipService.request_join, self.p1, self.squad)

    def test_a_block_reads_like_any_other_refusal(self):
        self.block(self.p1, target_org=self.org)

        # Player side: exactly what a turned-off link says.
        error = self.assert_raises_code(
            "invite_closed", MembershipService.request_join, self.p1, self.squad
        )
        self.assertEqual(error.message, "This invite link isn't available any more.")

        # Club side: the same generic sentence as a deactivated account.
        error = self.assert_raises_code(
            "cannot_add", MembershipService.invite, self.owner_actor(), self.squad.id, self.p1.id
        )
        self.assertEqual(error.message, CANNOT_ADD_MESSAGE)

        User.objects.filter(id=self.p2.id).update(is_active=False)
        error = self.assert_raises_code(
            "cannot_add", MembershipService.invite, self.owner_actor(), self.squad.id, self.p2.id
        )
        self.assertEqual(error.message, CANNOT_ADD_MESSAGE)

    def test_a_link_tap_with_an_open_invite_accepts_it(self):
        invited = self.invite(self.p1)
        row, created = self.request(self.p1)

        self.assertFalse(created)
        self.assertEqual(row.id, invited.id)
        self.assertEqual(row.status, Status.ACTIVE)
        self.assertEqual(self.squad_counts(), (1, 0))


class ApproveTests(MembershipTestBase):

    def test_bulk_approve(self):
        self.primary_position(self.p1, self.striker)
        rows = [self.request(user)[0] for user in (self.p1, self.p2, self.p3)]
        self.activate(self.make_squad("Seniors"), make_user("dina"), jersey_number=9)

        result = self.approve(
            self.squad,
            {"member_id": str(rows[0].id), "jersey_number": 9, "is_captain": True},
            {"member_id": str(rows[1].id), "jersey_number": 9},
            {"member_id": str(rows[2].id), "position_id": str(self.gk.id), "is_captain": True},
        )

        self.assertEqual([row.id for row in result["approved"]], [rows[0].id, rows[2].id])
        self.assertEqual(
            result["failed"],
            [{"member_id": str(rows[1].id), "code": "jersey_taken",
              "message": "Another player in this squad has that number."}],
        )

        first, second, third = (SquadMember.objects.get(id=row.id) for row in rows)
        # Prefilled from the player's own primary position.
        self.assertEqual(first.position, self.striker)
        self.assertEqual(third.position, self.gk)
        # The second captain replaced the first.
        self.assertFalse(first.is_captain)
        self.assertTrue(third.is_captain)
        self.assertEqual(second.status, Status.PENDING)
        self.assertEqual(self.squad_counts(), (2, 1))
        self.assertEqual(first.approved_by, self.owner_member)

    def test_a_request_that_is_no_longer_pending(self):
        row, _ = self.request(self.p1)
        self.approve(self.squad, {"member_id": str(row.id)})
        result = self.approve(self.squad, {"member_id": str(row.id)})
        self.assertEqual(result["failed"][0]["code"], "not_pending")

    def test_decline_deletes_the_request(self):
        row, _ = self.request(self.p1)
        result = self.run_after_commit(
            MembershipService.decline, self.coach_actor(), self.squad.id, [str(row.id), "junk"]
        )

        self.assertEqual(result["declined"], [str(row.id)])
        self.assertEqual(result["failed"][0]["code"], "not_pending")
        self.assertFalse(SquadMember.objects.filter(id=row.id).exists())
        self.assertEqual(self.squad_counts(), (0, 0))


class CareerEntryRuleTests(MembershipTestBase):

    def test_an_open_claim_at_the_club_is_reused_and_its_status_kept(self):
        for status in (Verification.PENDING, Verification.SELF_REPORTED):
            user = make_user(f"p{status}")
            claim = self.entry(user, status=status)

            row = self.join_squad(user)

            claim.refresh_from_db()
            self.assertEqual(row.career_entry, claim)
            self.assertFalse(row.career_entry_created)
            self.assertEqual(row.career_entry_prior_status, status)
            self.assertEqual(claim.verification_status, Verification.VERIFIED)
            self.assertEqual(claim.verified_by, self.owner)
            self.assertEqual((claim.squad_level, claim.age_group), ("youth", "U17"))

    def test_a_rejected_or_ended_claim_is_not_reused(self):
        self.entry(self.p1, status=Verification.REJECTED)
        self.entry(self.p1, status=Verification.PENDING, is_current=False, end_date=date(2025, 6, 1))

        row = self.join_squad(self.p1)

        self.assertTrue(row.career_entry_created)
        self.assertEqual(CareerEntry.objects.filter(user=self.p1).count(), 3)

    def test_no_claim_creates_a_verified_entry(self):
        row = self.join_squad(self.p1, position_id=str(self.defender.id))
        entry = row.career_entry

        self.assertTrue(row.career_entry_created)
        self.assertEqual(entry.source, CareerEntry.Source.SQUAD)
        self.assertEqual(entry.title, "Player")
        self.assertEqual(entry.entry_type, CareerEntry.EntryType.CLUB_TEAM)
        self.assertEqual(entry.verification_status, Verification.VERIFIED)
        self.assertTrue(entry.is_current)
        self.assertEqual(list(entry.positions.all()), [self.defender])

    def test_an_academy_makes_an_academy_entry(self):
        Organization.objects.filter(id=self.org.id).update(type=Organization.Type.ACADEMY)
        self.squad.organization.refresh_from_db()
        row = self.join_squad(self.p1)
        self.assertEqual(row.career_entry.entry_type, CareerEntry.EntryType.ACADEMY)

    def test_left_closes_the_entry_and_keeps_it_verified(self):
        row = self.join_squad(self.p1)
        self.run_after_commit(
            MembershipService.remove, self.coach_actor(), self.squad.id, row.id, "left"
        )

        row.refresh_from_db()
        entry = CareerEntry.objects.get(id=row.career_entry_id)
        self.assertEqual(row.status, Status.LEFT)
        self.assertEqual(row.ended_by, self.coach_member)
        self.assertFalse(entry.is_current)
        self.assertEqual(entry.end_date, timezone.localdate())
        self.assertEqual(entry.verification_status, Verification.VERIFIED)
        self.assertEqual(self.squad_counts(), (0, 0))

    def test_mistake_deletes_an_entry_it_created(self):
        row = self.join_squad(self.p1)
        entry_id = row.career_entry_id

        self.run_after_commit(
            MembershipService.remove, self.coach_actor(), self.squad.id, row.id, "mistake"
        )

        row.refresh_from_db()
        self.assertEqual(row.status, Status.REMOVED)
        self.assertIsNone(row.career_entry_id)
        self.assertFalse(CareerEntry.objects.filter(id=entry_id).exists())

    def test_mistake_restores_a_claims_prior_status(self):
        claim = self.entry(self.p1, status=Verification.PENDING)
        row = self.join_squad(self.p1)

        self.run_after_commit(
            MembershipService.remove, self.coach_actor(), self.squad.id, row.id, "mistake"
        )

        claim.refresh_from_db()
        self.assertEqual(claim.verification_status, Verification.PENDING)
        self.assertIsNone(claim.verified_by)
        self.assertIsNone(claim.verified_at)
        self.assertTrue(claim.is_current)

    def test_leaving_one_squad_while_in_another_of_the_club_keeps_the_entry_open(self):
        seniors = self.make_squad("Seniors")
        first = self.join_squad(self.p1)
        second = self.join_squad(self.p1, squad=seniors)
        self.assertEqual(first.career_entry_id, second.career_entry_id)

        self.run_after_commit(MembershipService.leave, self.p1, first.id)

        entry = CareerEntry.objects.get(id=first.career_entry_id)
        self.assertTrue(entry.is_current)

        self.run_after_commit(MembershipService.leave, self.p1, second.id)
        entry.refresh_from_db()
        self.assertFalse(entry.is_current)

    def test_a_move_keeps_the_entry_and_copies_the_new_age_group(self):
        seniors = self.make_squad("Seniors", age_group="Senior", level="senior")
        row = self.join_squad(self.p1, jersey_number=7, position_id=str(self.gk.id))
        self.activate(seniors, self.p2, jersey_number=7)

        moved = self.run_after_commit(
            MembershipService.move, self.coach_actor(), self.squad.id, row.id, seniors.id
        )

        row.refresh_from_db()
        entry = CareerEntry.objects.get(id=row.career_entry_id)
        self.assertEqual(row.status, Status.MOVED)
        self.assertEqual(moved.status, Status.ACTIVE)
        self.assertEqual(moved.career_entry_id, entry.id)
        self.assertEqual(moved.position, self.gk)
        self.assertIsNone(moved.jersey_number)  # 7 is taken there
        self.assertTrue(entry.is_current)
        self.assertEqual((entry.age_group, entry.squad_level), ("Senior", "senior"))
        self.assertEqual(self.squad_counts(), (0, 0))
        self.assertEqual(self.squad_counts(seniors)[0], 2)

    def test_a_move_copies_a_blank_age_group_and_level_too(self):
        # Moving from "Youth · U17" into a squad with neither must not leave
        # the stint reading "U17": the target's values go on as they are.
        plain = self.make_squad("Open Squad")
        row = self.join_squad(self.p1)
        entry = CareerEntry.objects.get(id=row.career_entry_id)
        self.assertEqual((entry.age_group, entry.squad_level), ("U17", "youth"))

        self.run_after_commit(
            MembershipService.move, self.coach_actor(), self.squad.id, row.id, plain.id
        )

        entry.refresh_from_db()
        self.assertEqual((entry.age_group, entry.squad_level), ("", ""))

    def test_a_move_copies_the_level_even_when_the_age_group_is_blank(self):
        seniors = self.make_squad("Seniors", level="senior")
        row = self.join_squad(self.p1)

        self.run_after_commit(
            MembershipService.move, self.coach_actor(), self.squad.id, row.id, seniors.id
        )

        entry = CareerEntry.objects.get(id=row.career_entry_id)
        self.assertEqual((entry.age_group, entry.squad_level), ("", "senior"))

    def test_move_refusals(self):
        row = self.join_squad(self.p1)
        cricket = self.make_squad("Cricket XI", sport=self.cricket)
        seniors = self.make_squad("Seniors")
        self.request(self.p1, seniors)

        for target, code in (
            (self.squad.id, "same_squad"),
            (cricket.id, "sport_mismatch"),
            (seniors.id, "already_requested"),
        ):
            self.assert_raises_code(
                code, MembershipService.move, self.coach_actor(), self.squad.id, row.id, target
            )


class DirectInviteTests(MembershipTestBase):

    def test_accept_creates_the_verified_entry(self):
        row = self.invite(self.p1, jersey_number=10)
        self.assertEqual(row.status, Status.INVITED)
        self.assertEqual(row.invited_by_member, self.owner_member)
        self.assertEqual(self.squad_counts(), (0, 0))

        row = self.accept(self.p1, row)

        self.assertEqual(row.status, Status.ACTIVE)
        self.assertEqual(row.approved_by, self.owner_member)
        self.assertEqual(row.career_entry.verification_status, Verification.VERIFIED)
        self.assertEqual(row.career_entry.verified_by, self.owner)
        self.assertEqual(self.squad_counts(), (1, 0))

    def test_an_expired_invite(self):
        row = self.invite(self.p1)
        SquadMember.objects.filter(id=row.id).update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assert_raises_code("invite_closed", MembershipService.accept_invite, self.p1, row.id)

        # And it no longer stands in the way of a fresh invite.
        fresh = self.invite(self.p1)
        self.assertNotEqual(fresh.id, row.id)
        self.assertFalse(SquadMember.objects.filter(id=row.id).exists())

    def test_an_inviter_who_lost_squads_review(self):
        row = self.invite(self.p1, actor=self.coach_actor())
        OrganizationMember.objects.filter(id=self.coach_member.id).update(
            role=OrganizationMember.Role.STAFF
        )
        self.assert_raises_code("invite_closed", MembershipService.accept_invite, self.p1, row.id)

    def test_inviting_someone_who_already_asked_approves_them(self):
        asked, _ = self.request(self.p1)
        row = self.invite(self.p1, jersey_number=4)

        self.assertEqual(row.id, asked.id)
        self.assertEqual(row.status, Status.ACTIVE)
        self.assertEqual(row.jersey_number, 4)
        self.assertEqual(self.squad_counts(), (1, 0))

    def test_already_member_and_already_invited(self):
        self.invite(self.p1)
        self.assert_raises_code("already_invited", MembershipService.invite, self.owner_actor(), self.squad.id, self.p1.id)
        self.join_squad(self.p2)
        self.assert_raises_code("already_member", MembershipService.invite, self.owner_actor(), self.squad.id, self.p2.id)

    def test_the_jersey_is_cleared_at_accept_when_taken_meanwhile(self):
        row = self.invite(self.p1, jersey_number=7)
        self.activate(self.squad, self.p2, jersey_number=7)

        row = self.accept(self.p1, row)
        self.assertEqual(row.status, Status.ACTIVE)
        self.assertIsNone(row.jersey_number)

    def test_cancel_and_decline_delete_the_invite(self):
        row = self.invite(self.p1)
        self.run_after_commit(MembershipService.cancel_invite, self.coach_actor(), self.squad.id, row.id)
        self.assertFalse(SquadMember.objects.filter(id=row.id).exists())

        row = self.invite(self.p1)
        self.run_after_commit(MembershipService.decline_invite, self.p1, row.id)
        self.assertFalse(SquadMember.objects.filter(id=row.id).exists())

    def test_invite_limit(self):
        with patch("apps.squads.services.membership_service.MAX_OPEN_INVITES_PER_SQUAD", 1):
            self.invite(self.p1)
            self.assert_raises_code("invite_limit", MembershipService.invite, self.owner_actor(), self.squad.id, self.p2.id)

    def test_member_limit(self):
        with patch("apps.squads.services.membership_service.MAX_ACTIVE_PER_SQUAD", 1):
            self.join_squad(self.p1)
            self.assert_raises_code("member_limit", MembershipService.invite, self.owner_actor(), self.squad.id, self.p2.id)


class RecruitmentInviteTests(MembershipTestBase):

    def _application(self, applicant, org=None, status="selected"):
        recruitment = Recruitment.objects.create(
            organization=org or self.org, sport=self.football, title="Open Trial",
            recruitment_type="open_trial", status=Recruitment.Status.ACTIVE,
            visibility=Recruitment.Visibility.PUBLIC,
        )
        return RecruitmentApplication.objects.create(
            recruitment=recruitment, applicant=applicant, shared_name="P",
            shared_phone="9999999999", status=status,
        )

    def test_a_selected_applicant(self):
        application = self._application(self.p1)
        row = self.invite(self.p1, application_id=str(application.id))

        self.assertEqual(row.source, SquadMember.Source.RECRUITMENT)
        self.assertEqual(row.recruitment_application, application)

        row = self.accept(self.p1, row)
        self.assertEqual(row.career_entry.recruitment_application, application)

    def test_only_this_orgs_selected_application_of_that_player(self):
        rival = Organization.objects.create(name="Rival", username="rival", type="club")
        OrganizationProfile.objects.create(organization=rival)

        for application in (
            self._application(self.p1, org=rival),
            self._application(self.p2),
            self._application(self.p1, status="applied"),
        ):
            self.assert_raises_code(
                "invalid_application", MembershipService.invite,
                self.owner_actor(), self.squad.id, self.p1.id,
                application_id=str(application.id),
            )


class UpdateMemberTests(MembershipTestBase):

    def test_edit_a_player(self):
        first = self.join_squad(self.p1, is_captain=True, jersey_number=1)
        second = self.join_squad(self.p2)

        row = self.run_after_commit(
            MembershipService.update_member, self.coach_actor(), self.squad.id, second.id,
            {"position_id": str(self.striker.id), "jersey_number": "10",
             "is_captain": True, "notes": "  Quick  "},
        )

        first.refresh_from_db()
        self.assertFalse(first.is_captain)
        self.assertTrue(row.is_captain)
        self.assertEqual((row.jersey_number, row.notes), (10, "Quick"))
        self.assertIn(self.striker, row.career_entry.positions.all())

        for payload, code in (
            ({"jersey_number": 1}, "jersey_taken"),
            ({"jersey_number": 100}, "invalid_jersey"),
            ({"position_id": str(self.batsman.id)}, "invalid_position"),
            ({"notes": "x" * 501}, "invalid_field"),
            ({"is_captain": "yes"}, "invalid_field"),
        ):
            self.assert_raises_code(
                code, MembershipService.update_member,
                self.coach_actor(), self.squad.id, second.id, payload,
            )

    def test_remove_needs_a_reason_and_an_active_player(self):
        row = self.join_squad(self.p1)
        self.assert_raises_code("invalid_reason", MembershipService.remove, self.coach_actor(), self.squad.id, row.id, "gone")
        self.run_after_commit(MembershipService.remove, self.coach_actor(), self.squad.id, row.id, "left")
        self.assert_raises_code("not_active", MembershipService.remove, self.coach_actor(), self.squad.id, row.id, "left")

    def test_a_reviewer_who_lost_access_mid_request(self):
        actor = self.coach_actor()
        OrganizationMember.objects.filter(id=self.coach_member.id).update(
            status=OrganizationMember.Status.REMOVED
        )
        from rest_framework.exceptions import PermissionDenied

        with self.assertRaisesMessage(PermissionDenied, "You can no longer manage this squad."):
            MembershipService.invite(actor, self.squad.id, self.p1.id)


class EndAllForUserTests(MembershipTestBase):

    def test_ends_every_membership_and_recounts(self):
        active = self.join_squad(self.p1)
        seniors = self.make_squad("Seniors")
        self.request(self.p1, seniors)
        third = self.make_squad("Thirds")
        self.invite(self.p1, squad=third)

        self.run_after_commit(MembershipService.end_all_for_user, self.p1)

        active.refresh_from_db()
        self.assertEqual(active.status, Status.LEFT)
        self.assertFalse(CareerEntry.objects.get(id=active.career_entry_id).is_current)
        self.assertEqual(
            SquadMember.objects.filter(user=self.p1).count(), 1
        )
        self.assertEqual(self.squad_counts(), (0, 0))
        self.assertEqual(self.squad_counts(seniors), (0, 0))
