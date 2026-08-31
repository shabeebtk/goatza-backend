"""
Service-level tests for career entries.

These call CareerEntryService / the selectors directly rather than going through
the API, because that is where the rules live — the views only translate
exceptions into the response envelope. The service takes a ``core.actor.Actor``,
so the tests build one the same way ``resolve_actor`` would.
"""

from datetime import date, timedelta

from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.exceptions import (
    NotFound,
    PermissionDenied,
    ValidationError,
)
from rest_framework.test import APITestCase

from accounts.models import User, UserProfile
from careers.models import CareerEntry
from careers.selectors.career_selectors import (
    career_entries_for,
    decided_verification_requests_for,
    pending_verification_requests_for,
)
from careers.serializers.career_serializers import (
    CareerEntrySerializer,
    CareerVerificationRequestSerializer,
)
from careers.services.career_services import CareerEntryService
from careers.services.career_verification_services import CareerVerificationService
from core.actor import Actor
from notifications.models import Notification
from notifications.services.grouping_service import NotificationGroupingService
from organization.models import (
    Organization,
    OrganizationMember,
    OrganizationProfile,
)
from recruitments.models import (
    Recruitment,
    RecruitmentApplication,
    RecruitmentApplicationStatusHistory,
)
from recruitments.services.application_service import ApplicationService
from sports.models import Sport, SportPosition
from legal.testing import accept_current_terms


@override_settings(
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"]
)
class CareerEntryServiceTestCase(TestCase):
    """Shared cast: one player, one other player, two sports, one club."""

    def setUp(self):
        self.player = self._user("player")
        self.other = self._user("other")

        self.football = Sport.objects.create(name="Football", icon_name="mdi:soccer")
        self.basketball = Sport.objects.create(name="Basketball", icon_name="mdi:basketball")

        self.striker = SportPosition.objects.create(sport=self.football, name="Striker")
        self.keeper = SportPosition.objects.create(sport=self.football, name="Goalkeeper")
        self.point_guard = SportPosition.objects.create(
            sport=self.basketball, name="Point Guard"
        )

        self.club = self._org("dreamfc", "Dream FC")

        self.actor = Actor(actor_type="user", user=self.player)

    # ── factories ────────────────────────────────────────────────

    def _user(self, username, role=User.Role.PLAYER):
        user = User.objects.create_user(
            email=f"{username}@example.com",
            password="pass1234",
            username=username,
            role=role,
        )
        accept_current_terms(user)
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

    def _org_actor(self, user, org, role=OrganizationMember.Role.OWNER):
        """What resolve_actor returns for a verified member acting as the org."""
        membership = OrganizationMember.objects.create(
            organization=org,
            user=user,
            role=role,
        )
        return Actor(
            actor_type="organization",
            organization=org,
            organization_member=membership,
        )

    def _payload(self, **overrides):
        payload = {
            "sport": self.football.id,
            "title": "Player",
            "organization_name": "Old Town FC",
            "start_date": date(2020, 1, 1),
            "end_date": date(2021, 1, 1),
        }
        payload.update(overrides)
        return payload

    def _create(self, **overrides):
        return CareerEntryService.create_entry(
            self.actor,
            payload=self._payload(**overrides)
        )

    def _verify(self, entry, by=None):
        """Put an entry into the state a club confirmation would leave it in."""
        entry.verification_status = CareerEntry.VerificationStatus.VERIFIED
        entry.verified_by = by or self.other
        entry.verified_at = timezone.now()
        entry.save()
        return entry


# =====================================================================
# CREATE
# =====================================================================

class CreateCareerEntryTests(CareerEntryServiceTestCase):

    def test_create_with_organization_is_pending(self):
        """A claim against a real club needs that club's confirmation."""
        entry = self._create(organization=self.club.id)

        self.assertEqual(
            entry.verification_status,
            CareerEntry.VerificationStatus.PENDING
        )
        self.assertEqual(entry.organization_id, self.club.id)
        # Synced from the org, not from whatever the client typed.
        self.assertEqual(entry.organization_name, "Dream FC")
        self.assertIsNone(entry.verified_by_id)
        self.assertIsNone(entry.verified_at)

    def test_create_with_free_text_is_self_reported(self):
        """Nobody on the platform can confirm a club that is not on it."""
        entry = self._create(organization_name="Old Town FC")

        self.assertEqual(
            entry.verification_status,
            CareerEntry.VerificationStatus.SELF_REPORTED
        )
        self.assertIsNone(entry.organization_id)
        self.assertEqual(entry.organization_name, "Old Town FC")

    def test_create_requires_an_organization_or_a_name(self):
        with self.assertRaises(ValidationError):
            self._create(organization_name="")

    def test_create_stores_positions(self):
        entry = self._create(positions=[self.striker.id, self.keeper.id])

        self.assertEqual(
            set(entry.positions.values_list("id", flat=True)),
            {self.striker.id, self.keeper.id},
        )

    def test_create_rejects_position_from_another_sport(self):
        """A Point Guard is not a Football position."""
        with self.assertRaises(ValidationError) as ctx:
            self._create(positions=[self.striker.id, self.point_guard.id])

        self.assertIn("Point Guard", str(ctx.exception.detail))
        self.assertEqual(CareerEntry.objects.count(), 0)

    def test_create_rejects_unknown_sport_and_organization(self):
        missing = "00000000-0000-0000-0000-000000000001"

        with self.assertRaises(ValidationError):
            self._create(sport=missing)

        with self.assertRaises(ValidationError):
            self._create(organization=missing)

    def test_is_current_clears_end_date(self):
        entry = self._create(is_current=True, end_date=date(2021, 1, 1))

        self.assertTrue(entry.is_current)
        self.assertIsNone(entry.end_date)

    def test_end_date_before_start_date_is_rejected(self):
        with self.assertRaises(ValidationError):
            self._create(start_date=date(2021, 1, 1), end_date=date(2020, 1, 1))

    def test_entries_are_capped(self):
        """A career is a highlight reel — MAX_ENTRIES bounds it."""
        for index in range(CareerEntryService.MAX_ENTRIES):
            self._create(title=f"role {index}")

        with self.assertRaises(ValidationError) as ctx:
            self._create(title="one too many")

        self.assertIn("10", str(ctx.exception.detail))
        self.assertEqual(
            CareerEntry.objects.filter(user=self.player).count(),
            CareerEntryService.MAX_ENTRIES,
        )

    def test_the_cap_is_per_user(self):
        for index in range(CareerEntryService.MAX_ENTRIES):
            self._create(title=f"role {index}")

        # Someone else is unaffected.
        other = CareerEntryService.create_entry(
            Actor(actor_type="user", user=self.other),
            payload=self._payload(),
        )
        self.assertIsNotNone(other.id)

    def test_deleting_frees_a_slot(self):
        entries = [
            self._create(title=f"role {i}")
            for i in range(CareerEntryService.MAX_ENTRIES)
        ]

        CareerEntryService.delete_entry(self.actor, entries[0].id)

        self._create(title="replacement")
        self.assertEqual(
            CareerEntry.objects.filter(user=self.player).count(),
            CareerEntryService.MAX_ENTRIES,
        )

    def test_organization_actor_cannot_create(self):
        """Careers belong to a person; an org actor is refused outright."""
        org_actor = self._org_actor(self.player, self.club)

        with self.assertRaises(PermissionDenied):
            CareerEntryService.create_entry(org_actor, payload=self._payload())

        self.assertEqual(CareerEntry.objects.count(), 0)


# =====================================================================
# UPDATE
# =====================================================================

class UpdateCareerEntryTests(CareerEntryServiceTestCase):

    def test_material_edit_on_verified_entry_resets_to_pending(self):
        entry = self._verify(self._create())

        updated = CareerEntryService.update_entry(
            self.actor,
            entry.id,
            payload={"title": "Captain"}
        )

        self.assertEqual(updated.title, "Captain")
        self.assertEqual(
            updated.verification_status,
            CareerEntry.VerificationStatus.PENDING
        )
        self.assertIsNone(updated.verified_by_id)
        self.assertIsNone(updated.verified_at)

    def test_position_change_on_verified_entry_resets_to_pending(self):
        """positions is a material field even though it is not a column."""
        entry = self._verify(self._create(positions=[self.striker.id]))

        updated = CareerEntryService.update_entry(
            self.actor,
            entry.id,
            payload={"positions": [self.keeper.id]}
        )

        self.assertEqual(
            updated.verification_status,
            CareerEntry.VerificationStatus.PENDING
        )
        self.assertEqual(
            set(updated.positions.values_list("id", flat=True)),
            {self.keeper.id},
        )

    def test_description_only_edit_keeps_verified(self):
        """Rewording your own blurb does not invalidate a club's confirmation."""
        entry = self._verify(self._create())
        verified_at = entry.verified_at

        updated = CareerEntryService.update_entry(
            self.actor,
            entry.id,
            payload={"description": "Won the league."}
        )

        self.assertEqual(updated.description, "Won the league.")
        self.assertEqual(
            updated.verification_status,
            CareerEntry.VerificationStatus.VERIFIED
        )
        self.assertEqual(updated.verified_by_id, self.other.id)
        self.assertEqual(updated.verified_at, verified_at)

    def test_resending_the_same_values_keeps_verified(self):
        """A client PATCHing the whole form back has not changed anything."""
        entry = self._verify(self._create(positions=[self.striker.id]))

        updated = CareerEntryService.update_entry(
            self.actor,
            entry.id,
            payload={
                "title": entry.title,
                "sport": entry.sport_id,
                "positions": [self.striker.id],
                "start_date": entry.start_date,
                "end_date": entry.end_date,
            }
        )

        self.assertEqual(
            updated.verification_status,
            CareerEntry.VerificationStatus.VERIFIED
        )

    def test_linking_an_organization_moves_to_pending(self):
        entry = self._create(organization_name="Old Town FC")

        updated = CareerEntryService.update_entry(
            self.actor,
            entry.id,
            payload={"organization": self.club.id}
        )

        self.assertEqual(
            updated.verification_status,
            CareerEntry.VerificationStatus.PENDING
        )
        self.assertEqual(updated.organization_name, "Dream FC")

    def test_unlinking_an_organization_falls_back_to_self_reported(self):
        entry = self._verify(self._create(organization=self.club.id))

        updated = CareerEntryService.update_entry(
            self.actor,
            entry.id,
            payload={
                "organization": None,
                "organization_name": "Dream FC (old name)",
            }
        )

        self.assertIsNone(updated.organization_id)
        self.assertEqual(
            updated.verification_status,
            CareerEntry.VerificationStatus.SELF_REPORTED
        )
        self.assertEqual(updated.organization_name, "Dream FC (old name)")

    def test_unlinking_without_a_name_is_rejected(self):
        entry = self._create(organization=self.club.id)

        # The synced "Dream FC" is still on the row, so an unlink alone is fine;
        # blanking the name at the same time leaves nothing to display.
        with self.assertRaises(ValidationError):
            CareerEntryService.update_entry(
                self.actor,
                entry.id,
                payload={"organization": None, "organization_name": ""}
            )

    def test_changing_sport_rejects_stale_positions(self):
        entry = self._create(positions=[self.striker.id])

        with self.assertRaises(ValidationError):
            CareerEntryService.update_entry(
                self.actor,
                entry.id,
                payload={
                    "sport": self.basketball.id,
                    "positions": [self.striker.id],
                }
            )

    def test_changing_sport_without_positions_clears_them(self):
        entry = self._create(positions=[self.striker.id])

        updated = CareerEntryService.update_entry(
            self.actor,
            entry.id,
            payload={"sport": self.basketball.id}
        )

        self.assertEqual(updated.sport_id, self.basketball.id)
        self.assertEqual(updated.positions.count(), 0)

    def test_marking_current_clears_the_end_date(self):
        entry = self._create(end_date=date(2021, 1, 1))

        updated = CareerEntryService.update_entry(
            self.actor,
            entry.id,
            payload={"is_current": True}
        )

        self.assertTrue(updated.is_current)
        self.assertIsNone(updated.end_date)

    def test_cannot_update_someone_elses_entry(self):
        entry = self._create()
        stranger = Actor(actor_type="user", user=self.other)

        with self.assertRaises(PermissionDenied):
            CareerEntryService.update_entry(
                stranger,
                entry.id,
                payload={"title": "Captain"}
            )

    def test_organization_actor_cannot_update(self):
        entry = self._create()
        org_actor = self._org_actor(self.player, self.club)

        with self.assertRaises(PermissionDenied):
            CareerEntryService.update_entry(
                org_actor,
                entry.id,
                payload={"title": "Captain"}
            )


# =====================================================================
# DELETE
# =====================================================================

class DeleteCareerEntryTests(CareerEntryServiceTestCase):

    def test_delete_removes_the_row(self):
        """Hard delete — structured profile data, not user-authored content."""
        entry = self._create()

        deleted_id = CareerEntryService.delete_entry(self.actor, entry.id)

        self.assertEqual(deleted_id, entry.id)
        self.assertFalse(CareerEntry.objects.filter(id=entry.id).exists())

    def test_cannot_delete_someone_elses_entry(self):
        entry = self._create()
        stranger = Actor(actor_type="user", user=self.other)

        with self.assertRaises(PermissionDenied):
            CareerEntryService.delete_entry(stranger, entry.id)

        self.assertTrue(CareerEntry.objects.filter(id=entry.id).exists())

    def test_missing_entry_is_not_found(self):
        with self.assertRaises(NotFound):
            CareerEntryService.delete_entry(
                self.actor,
                "00000000-0000-0000-0000-000000000001"
            )


# =====================================================================
# ORDERING
# =====================================================================

class CareerEntryOrderingTests(CareerEntryServiceTestCase):

    def test_list_ordering(self):
        """
        is_current DESC, then COALESCE(end_date, today) DESC, then start_date
        DESC — so a still-running open-ended spell outranks one that ended
        years ago, and two entries sharing an end date fall back to the later
        start (the loan above the contract it sat inside).
        """
        old = self._create(
            title="old",
            start_date=date(2015, 1, 1),
            end_date=date(2016, 1, 1),
        )
        recent = self._create(
            title="recent",
            start_date=date(2018, 1, 1),
            end_date=date(2020, 1, 1),
        )
        # No end date and not flagged current — COALESCE puts it at today.
        open_ended = self._create(
            title="open",
            start_date=date(2017, 1, 1),
            end_date=None,
        )
        # Shares recent's end date, started later → sorts above it.
        loan = self._create(
            title="loan",
            start_date=date(2019, 6, 1),
            end_date=date(2020, 1, 1),
        )
        current = self._create(
            title="current",
            start_date=date(2014, 1, 1),
            is_current=True,
        )

        titles = [
            entry.title
            for entry in career_entries_for(self.player)
        ]

        self.assertEqual(
            titles,
            ["current", "open", "loan", "recent", "old"],
        )

    def test_ordering_is_per_user(self):
        self._create(title="mine")

        CareerEntryService.create_entry(
            Actor(actor_type="user", user=self.other),
            payload=self._payload(title="theirs")
        )

        titles = [entry.title for entry in career_entries_for(self.player)]

        self.assertEqual(titles, ["mine"])


# =====================================================================
# ORG VERIFICATION
# =====================================================================

class CareerVerificationTestCase(CareerEntryServiceTestCase):
    """
    Adds the reviewer cast: an owner and a coach at the tagged club, plus a
    second club that has nothing to do with the entry.
    """

    def setUp(self):
        super().setUp()

        self.club_owner = self._user("clubowner", User.Role.ORG_USER)
        self.club_coach = self._user("clubcoach", User.Role.COACH)
        self.club_staff = self._user("clubstaff", User.Role.ORG_USER)

        self.owner_actor = self._org_actor(
            self.club_owner, self.club, OrganizationMember.Role.OWNER
        )
        self.coach_actor = self._org_actor(
            self.club_coach, self.club, OrganizationMember.Role.COACH
        )
        self.staff_actor = self._org_actor(
            self.club_staff, self.club, OrganizationMember.Role.STAFF
        )

        # A different club, with its own owner.
        self.rival = self._org("rivalfc", "Rival FC")
        self.rival_owner = self._user("rivalowner", User.Role.ORG_USER)
        self.rival_actor = self._org_actor(
            self.rival_owner, self.rival, OrganizationMember.Role.OWNER
        )

    def _pending_entry(self):
        """An entry tagging self.club, sitting in the club's review queue."""
        with self.captureOnCommitCallbacks(execute=True):
            return self._create(organization=self.club.id)

    def _requests(self):
        return Notification.objects.filter(
            type=Notification.Type.CAREER_VERIFICATION_REQUEST
        )


class CareerVerificationPermissionTests(CareerVerificationTestCase):

    def test_wrong_org_is_rejected(self):
        """A club cannot act on an entry that names a different club."""
        entry = self._pending_entry()

        with self.assertRaises(PermissionDenied):
            CareerVerificationService.verify_entry(self.rival_actor, entry.id)

        with self.assertRaises(PermissionDenied):
            CareerVerificationService.reject_entry(self.rival_actor, entry.id)

        entry.refresh_from_db()
        self.assertEqual(
            entry.verification_status,
            CareerEntry.VerificationStatus.PENDING
        )

    def test_coach_member_is_rejected(self):
        entry = self._pending_entry()

        with self.assertRaises(PermissionDenied):
            CareerVerificationService.verify_entry(self.coach_actor, entry.id)

        entry.refresh_from_db()
        self.assertEqual(
            entry.verification_status,
            CareerEntry.VerificationStatus.PENDING
        )

    def test_staff_member_is_rejected(self):
        entry = self._pending_entry()

        with self.assertRaises(PermissionDenied):
            CareerVerificationService.reject_entry(self.staff_actor, entry.id)

        entry.refresh_from_db()
        self.assertEqual(
            entry.verification_status,
            CareerEntry.VerificationStatus.PENDING
        )

    def test_user_actor_is_rejected(self):
        """Not even the entry's own owner can verify their own claim."""
        entry = self._pending_entry()

        with self.assertRaises(PermissionDenied):
            CareerVerificationService.verify_entry(self.actor, entry.id)

    def test_admin_member_is_allowed(self):
        entry = self._pending_entry()
        admin = self._user("clubadmin", User.Role.ORG_USER)
        admin_actor = self._org_actor(
            admin, self.club, OrganizationMember.Role.ADMIN
        )

        updated = CareerVerificationService.verify_entry(admin_actor, entry.id)

        self.assertEqual(
            updated.verification_status,
            CareerEntry.VerificationStatus.VERIFIED
        )

    def test_review_queue_is_scoped_to_the_acting_org(self):
        entry = self._pending_entry()

        self.assertEqual(
            [e.id for e in pending_verification_requests_for(self.club)],
            [entry.id],
        )
        self.assertEqual(
            list(pending_verification_requests_for(self.rival)),
            [],
        )

    def test_review_queue_excludes_decided_entries(self):
        entry = self._pending_entry()
        CareerVerificationService.verify_entry(self.owner_actor, entry.id)

        self.assertEqual(list(pending_verification_requests_for(self.club)), [])


class CareerVerificationTransitionTests(CareerVerificationTestCase):

    def test_verify_transition(self):
        entry = self._pending_entry()

        with self.captureOnCommitCallbacks(execute=True):
            updated = CareerVerificationService.verify_entry(
                self.owner_actor, entry.id
            )

        self.assertEqual(
            updated.verification_status,
            CareerEntry.VerificationStatus.VERIFIED
        )
        # The deciding PERSON, not the org — the trail survives them leaving.
        self.assertEqual(updated.verified_by_id, self.club_owner.id)
        self.assertIsNotNone(updated.verified_at)

        notification = Notification.objects.get(
            type=Notification.Type.CAREER_VERIFIED
        )
        self.assertEqual(notification.recipient_user_id, self.player.id)
        self.assertEqual(notification.actor_org_id, self.club.id)
        self.assertEqual(notification.career_entry_id, entry.id)

    def test_reject_transition_carries_the_reason(self):
        entry = self._pending_entry()

        with self.captureOnCommitCallbacks(execute=True):
            updated = CareerVerificationService.reject_entry(
                self.owner_actor,
                entry.id,
                reason="No record of this player in our squad lists."
            )

        self.assertEqual(
            updated.verification_status,
            CareerEntry.VerificationStatus.REJECTED
        )
        # Nobody verified anything, so the audit fields stay empty.
        self.assertIsNone(updated.verified_by_id)
        self.assertIsNone(updated.verified_at)

        notification = Notification.objects.get(
            type=Notification.Type.CAREER_REJECTED
        )
        self.assertEqual(notification.recipient_user_id, self.player.id)
        self.assertEqual(
            notification.data["reason"],
            "No record of this player in our squad lists."
        )

    def test_reject_reason_is_optional(self):
        entry = self._pending_entry()

        with self.captureOnCommitCallbacks(execute=True):
            CareerVerificationService.reject_entry(self.owner_actor, entry.id)

        notification = Notification.objects.get(
            type=Notification.Type.CAREER_REJECTED
        )
        self.assertEqual(notification.data["reason"], "")

    def test_overlong_reason_is_rejected(self):
        entry = self._pending_entry()

        with self.assertRaises(ValidationError):
            CareerVerificationService.reject_entry(
                self.owner_actor, entry.id, reason="x" * 201
            )

    def test_cannot_verify_twice(self):
        """A no-op decision is refused — that is a double-submit, not intent."""
        entry = self._pending_entry()
        CareerVerificationService.verify_entry(self.owner_actor, entry.id)

        with self.assertRaises(ValidationError) as ctx:
            CareerVerificationService.verify_entry(self.owner_actor, entry.id)

        self.assertIn("already verified", str(ctx.exception.detail).lower())

    def test_cannot_reject_twice(self):
        entry = self._pending_entry()
        CareerVerificationService.reject_entry(self.owner_actor, entry.id)

        with self.assertRaises(ValidationError) as ctx:
            CareerVerificationService.reject_entry(self.owner_actor, entry.id)

        self.assertIn("already rejected", str(ctx.exception.detail).lower())

    def test_a_club_can_verify_what_it_previously_rejected(self):
        """Clubs learn things late — history is revisitable, not final."""
        entry = self._pending_entry()
        CareerVerificationService.reject_entry(
            self.owner_actor, entry.id, reason="No record"
        )

        with self.captureOnCommitCallbacks(execute=True):
            updated = CareerVerificationService.verify_entry(
                self.owner_actor, entry.id
            )

        self.assertEqual(
            updated.verification_status,
            CareerEntry.VerificationStatus.VERIFIED
        )
        self.assertEqual(updated.verified_by_id, self.club_owner.id)
        self.assertTrue(
            Notification.objects.filter(
                type=Notification.Type.CAREER_VERIFIED,
                recipient_user=self.player,
            ).exists()
        )

    def test_a_club_can_withdraw_a_verification(self):
        entry = self._pending_entry()
        CareerVerificationService.verify_entry(self.owner_actor, entry.id)

        updated = CareerVerificationService.reject_entry(
            self.owner_actor, entry.id, reason="Withdrawn after review"
        )

        self.assertEqual(
            updated.verification_status,
            CareerEntry.VerificationStatus.REJECTED
        )
        # The old confirmation's audit trail must not survive the withdrawal.
        self.assertIsNone(updated.verified_by_id)
        self.assertIsNone(updated.verified_at)

    def test_owner_can_still_edit_a_rejected_entry(self):
        """A rejection is not a lock — the entry stays the player's to fix."""
        entry = self._pending_entry()
        CareerVerificationService.reject_entry(self.owner_actor, entry.id)

        updated = CareerEntryService.update_entry(
            self.actor,
            entry.id,
            payload={"title": "Reserve Player"}
        )

        self.assertEqual(updated.title, "Reserve Player")


class CareerVerificationNotificationTests(CareerVerificationTestCase):

    def test_create_with_org_notifies_the_org(self):
        with self.captureOnCommitCallbacks(execute=True):
            entry = self._create(organization=self.club.id)

        notification = self._requests().get()
        self.assertEqual(notification.recipient_org_id, self.club.id)
        self.assertEqual(notification.actor_user_id, self.player.id)
        self.assertEqual(notification.career_entry_id, entry.id)

    def test_free_text_entry_notifies_nobody(self):
        with self.captureOnCommitCallbacks(execute=True):
            self._create(organization_name="Old Town FC")

        self.assertEqual(self._requests().count(), 0)

    def test_material_edit_of_a_verified_entry_re_requests(self):
        """The headline case: a verified entry edited back into the queue."""
        entry = self._pending_entry()
        self.assertEqual(self._requests().count(), 1)

        CareerVerificationService.verify_entry(self.owner_actor, entry.id)

        with self.captureOnCommitCallbacks(execute=True):
            updated = CareerEntryService.update_entry(
                self.actor,
                entry.id,
                payload={"title": "Captain"}
            )

        self.assertEqual(
            updated.verification_status,
            CareerEntry.VerificationStatus.PENDING
        )
        self.assertEqual(self._requests().count(), 2)
        # And it is back in the club's queue.
        self.assertEqual(
            [e.id for e in pending_verification_requests_for(self.club)],
            [entry.id],
        )

    def test_description_edit_of_a_verified_entry_does_not_re_request(self):
        entry = self._pending_entry()
        CareerVerificationService.verify_entry(self.owner_actor, entry.id)

        with self.captureOnCommitCallbacks(execute=True):
            CareerEntryService.update_entry(
                self.actor,
                entry.id,
                payload={"description": "Two promotions."}
            )

        self.assertEqual(self._requests().count(), 1)

    def test_edit_of_an_already_pending_entry_does_not_re_request(self):
        """Still pending, still the same club — nothing new for the reviewer."""
        entry = self._pending_entry()

        with self.captureOnCommitCallbacks(execute=True):
            CareerEntryService.update_entry(
                self.actor,
                entry.id,
                payload={"title": "Captain"}
            )

        self.assertEqual(self._requests().count(), 1)

    def test_re_tagging_moves_the_request_to_the_new_club(self):
        """
        The old club is no longer being asked anything, so its request goes
        away — both from its queue (which reads through the FK) and from its
        notifications (which would otherwise linger as a dead invitation).
        """
        entry = self._pending_entry()
        self.assertEqual(self._requests().count(), 1)

        with self.captureOnCommitCallbacks(execute=True):
            CareerEntryService.update_entry(
                self.actor,
                entry.id,
                payload={"organization": self.rival.id}
            )

        self.assertEqual(
            list(self._requests().values_list("recipient_org_id", flat=True)),
            [self.rival.id],
        )
        # …and the queues agree with the notifications.
        self.assertEqual(list(pending_verification_requests_for(self.club)), [])
        self.assertEqual(
            [e.id for e in pending_verification_requests_for(self.rival)],
            [entry.id],
        )

    def test_unlinking_withdraws_the_request(self):
        entry = self._pending_entry()

        with self.captureOnCommitCallbacks(execute=True):
            CareerEntryService.update_entry(
                self.actor,
                entry.id,
                payload={
                    "organization": None,
                    "organization_name": "Dream FC (old)",
                },
            )

        self.assertEqual(self._requests().count(), 0)
        self.assertEqual(list(pending_verification_requests_for(self.club)), [])

    def test_a_decided_entry_leaves_requests_and_enters_history(self):
        """The two tabs partition the club's entries — never both, never neither."""
        entry = self._pending_entry()
        CareerVerificationService.verify_entry(self.owner_actor, entry.id)

        self.assertEqual(list(pending_verification_requests_for(self.club)), [])
        self.assertEqual(
            [e.id for e in decided_verification_requests_for(self.club)],
            [entry.id],
        )

    def test_editing_a_verified_entry_moves_it_back_to_requests(self):
        entry = self._pending_entry()
        CareerVerificationService.verify_entry(self.owner_actor, entry.id)

        with self.captureOnCommitCallbacks(execute=True):
            CareerEntryService.update_entry(
                self.actor, entry.id, payload={"title": "Captain"}
            )

        self.assertEqual(
            [e.id for e in pending_verification_requests_for(self.club)],
            [entry.id],
        )
        self.assertEqual(list(decided_verification_requests_for(self.club)), [])

    def test_deleting_an_entry_takes_its_notifications_with_it(self):
        """Careers hard-delete, so the FK cascade is what prevents dead links."""
        entry = self._pending_entry()
        self.assertEqual(self._requests().count(), 1)

        CareerEntryService.delete_entry(self.actor, entry.id)

        self.assertEqual(self._requests().count(), 0)


# =====================================================================
# RECRUITMENT INTEGRATION
# =====================================================================

class CareerFromApplicationTestCase(CareerEntryServiceTestCase):
    """
    Adds a recruitment the player applied to and an org member to run the
    pipeline with.
    """

    def setUp(self):
        super().setUp()

        self.club_owner = self._user("clubowner", User.Role.ORG_USER)
        self.owner_actor = self._org_actor(
            self.club_owner, self.club, OrganizationMember.Role.OWNER
        )
        self.member = self.owner_actor.organization_member

        self.recruitment = Recruitment.objects.create(
            organization=self.club,
            sport=self.football,
            title="U19 Open Trial",
            recruitment_type=Recruitment.Type.OPEN_TRIAL,
            status=Recruitment.Status.ACTIVE,
        )

    def _application(self, applicant=None, position=None, **kwargs):
        return RecruitmentApplication.objects.create(
            recruitment=kwargs.pop("recruitment", self.recruitment),
            applicant=applicant or self.player,
            shared_name="Player",
            shared_phone="9999999999",
            applied_position=position,
            **kwargs,
        )

    def _select(self, application, changed_by=None, note=""):
        """Move an application to selected the way the org pipeline does."""
        application.status = RecruitmentApplication.Status.SELECTED
        application.reviewed_by = changed_by or self.member
        application.reviewed_at = timezone.now()
        application.save(update_fields=["status", "reviewed_by", "reviewed_at"])

        RecruitmentApplicationStatusHistory.objects.create(
            application=application,
            from_status=RecruitmentApplication.Status.SHORTLISTED,
            to_status=RecruitmentApplication.Status.SELECTED,
            changed_by=changed_by or self.member,
            note=note,
        )
        return application


class CareerFromApplicationPrefillTests(CareerFromApplicationTestCase):

    def test_prefill_from_the_recruitment(self):
        application = self._select(self._application(position=self.striker))

        entry, created = CareerEntryService.create_from_application(
            self.actor, application.id
        )

        self.assertTrue(created)
        self.assertEqual(entry.user_id, self.player.id)
        self.assertEqual(entry.organization_id, self.club.id)
        self.assertEqual(entry.organization_name, "Dream FC")
        self.assertEqual(entry.sport_id, self.football.id)
        self.assertEqual(
            list(entry.positions.values_list("id", flat=True)),
            [self.striker.id],
        )
        self.assertEqual(entry.title, "Player")
        self.assertEqual(entry.entry_type, CareerEntry.EntryType.CLUB_TEAM)
        self.assertTrue(entry.is_current)
        self.assertIsNone(entry.end_date)
        self.assertEqual(entry.source, CareerEntry.Source.RECRUITMENT)
        self.assertEqual(entry.recruitment_application_id, application.id)

        # It came out of the org's own pipeline, so it arrives verified.
        self.assertEqual(
            entry.verification_status,
            CareerEntry.VerificationStatus.VERIFIED
        )
        self.assertIsNotNone(entry.verified_at)
        # …credited to the member who moved it to selected.
        self.assertEqual(entry.verified_by_id, self.club_owner.id)

    def test_no_applied_position_leaves_positions_empty(self):
        application = self._select(self._application())

        entry, _ = CareerEntryService.create_from_application(
            self.actor, application.id
        )

        self.assertEqual(entry.positions.count(), 0)

    def test_scholarship_recruitment_maps_to_academy(self):
        scholarship = Recruitment.objects.create(
            organization=self.club,
            sport=self.football,
            title="Academy Scholarship",
            recruitment_type=Recruitment.Type.SCHOLARSHIP,
            status=Recruitment.Status.ACTIVE,
        )
        application = self._select(
            self._application(recruitment=scholarship)
        )

        entry, _ = CareerEntryService.create_from_application(
            self.actor, application.id
        )

        self.assertEqual(entry.entry_type, CareerEntry.EntryType.ACADEMY)

    def test_start_date_prefers_the_event_date(self):
        self.recruitment.event_date = timezone.now() - timedelta(days=30)
        self.recruitment.save(update_fields=["event_date"])
        application = self._select(self._application())

        entry, _ = CareerEntryService.create_from_application(
            self.actor, application.id
        )

        self.assertEqual(
            entry.start_date,
            timezone.localdate(self.recruitment.event_date),
        )

    def test_start_date_falls_back_to_the_selection_date(self):
        application = self._select(self._application())

        entry, _ = CareerEntryService.create_from_application(
            self.actor, application.id
        )

        self.assertEqual(entry.start_date, timezone.localdate())

    def test_start_date_falls_back_to_today_without_history(self):
        """A status set by a path that wrote no history still works."""
        application = self._application()
        application.status = RecruitmentApplication.Status.SELECTED
        application.save(update_fields=["status"])

        entry, _ = CareerEntryService.create_from_application(
            self.actor, application.id
        )

        self.assertEqual(entry.start_date, timezone.localdate())
        # Nobody to credit — the history row that names them does not exist.
        self.assertIsNone(entry.verified_by_id)

    def test_overrides_are_applied_and_validated(self):
        application = self._select(self._application())

        entry, _ = CareerEntryService.create_from_application(
            self.actor,
            application.id,
            payload={
                "title": "Captain",
                "start_date": date(2019, 8, 1),
                "description": "Signed after the trial.",
            },
        )

        self.assertEqual(entry.title, "Captain")
        self.assertEqual(entry.start_date, date(2019, 8, 1))
        self.assertEqual(entry.description, "Signed after the trial.")

    def test_blank_title_override_is_rejected(self):
        application = self._select(self._application())

        with self.assertRaises(ValidationError):
            CareerEntryService.create_from_application(
                self.actor, application.id, payload={"title": "   "}
            )


class CareerFromApplicationGuardTests(CareerFromApplicationTestCase):

    def test_idempotent(self):
        application = self._select(self._application())

        first, created_first = CareerEntryService.create_from_application(
            self.actor, application.id
        )
        second, created_second = CareerEntryService.create_from_application(
            self.actor, application.id
        )

        self.assertTrue(created_first)
        self.assertFalse(created_second)
        self.assertEqual(first.id, second.id)
        self.assertEqual(CareerEntry.objects.count(), 1)

    def test_idempotent_even_after_the_application_moves_off_selected(self):
        """Asking again for an entry you already have must not start failing."""
        application = self._select(self._application())
        entry, _ = CareerEntryService.create_from_application(
            self.actor, application.id
        )

        application.status = RecruitmentApplication.Status.REJECTED
        application.save(update_fields=["status"])

        again, created = CareerEntryService.create_from_application(
            self.actor, application.id
        )

        self.assertFalse(created)
        self.assertEqual(again.id, entry.id)

    def test_wrong_user_is_rejected(self):
        application = self._select(self._application())
        stranger = Actor(actor_type="user", user=self.other)

        with self.assertRaises(PermissionDenied):
            CareerEntryService.create_from_application(stranger, application.id)

        self.assertEqual(CareerEntry.objects.count(), 0)

    def test_org_actor_is_rejected(self):
        application = self._select(self._application())

        with self.assertRaises(PermissionDenied):
            CareerEntryService.create_from_application(
                self.owner_actor, application.id
            )

    def test_application_not_selected_is_rejected(self):
        application = self._application()   # still `applied`

        with self.assertRaises(ValidationError) as ctx:
            CareerEntryService.create_from_application(self.actor, application.id)

        self.assertIn("selected", str(ctx.exception.detail).lower())
        self.assertEqual(CareerEntry.objects.count(), 0)

    def test_unknown_application_is_not_found(self):
        with self.assertRaises(NotFound):
            CareerEntryService.create_from_application(
                self.actor, "00000000-0000-0000-0000-000000000001"
            )


class CareerFromApplicationEditTests(CareerFromApplicationTestCase):

    def test_material_edit_drops_it_back_to_pending_and_re_notifies(self):
        """Stage 2's rule applies unchanged to a pipeline-created entry."""
        application = self._select(self._application())
        entry, _ = CareerEntryService.create_from_application(
            self.actor, application.id
        )

        with self.captureOnCommitCallbacks(execute=True):
            updated = CareerEntryService.update_entry(
                self.actor,
                entry.id,
                payload={"title": "Captain"},
            )

        self.assertEqual(
            updated.verification_status,
            CareerEntry.VerificationStatus.PENDING
        )
        self.assertIsNone(updated.verified_by_id)
        self.assertIsNone(updated.verified_at)

        self.assertEqual(
            Notification.objects.filter(
                type=Notification.Type.CAREER_VERIFICATION_REQUEST,
                recipient_org=self.club,
            ).count(),
            1,
        )

    def test_creating_from_an_application_does_not_notify_the_org(self):
        """The org already decided this by selecting them."""
        application = self._select(self._application())

        with self.captureOnCommitCallbacks(execute=True):
            CareerEntryService.create_from_application(self.actor, application.id)

        self.assertEqual(
            Notification.objects.filter(
                type=Notification.Type.CAREER_VERIFICATION_REQUEST
            ).count(),
            0,
        )


class CareerAddPromptTests(CareerFromApplicationTestCase):
    """The prompt rides on the existing status-change service."""

    def _change_status(self, application, to_status):
        with self.captureOnCommitCallbacks(execute=True):
            return ApplicationService.change_status(
                self.owner_actor,
                self.recruitment,
                [str(application.id)],
                to_status,
            )

    def _prompts(self):
        return Notification.objects.filter(
            type=Notification.Type.CAREER_ADD_PROMPT
        )

    def test_selection_sends_the_prompt_alongside_the_status_notification(self):
        application = self._application()

        result = self._change_status(
            application, RecruitmentApplication.Status.SELECTED
        )

        self.assertEqual(result["updated"], [str(application.id)])

        prompt = self._prompts().get()
        self.assertEqual(prompt.recipient_user_id, self.player.id)
        self.assertEqual(prompt.actor_org_id, self.club.id)
        self.assertEqual(prompt.recruitment_id, self.recruitment.id)
        self.assertEqual(prompt.data["application_id"], str(application.id))

        # The existing selection notification still goes out.
        self.assertTrue(
            Notification.objects.filter(
                type=Notification.Type.RECRUITMENT_APPLICATION_STATUS,
                recipient_user=self.player,
            ).exists()
        )

    def test_other_statuses_send_no_prompt(self):
        application = self._application()

        self._change_status(
            application, RecruitmentApplication.Status.SHORTLISTED
        )

        self.assertEqual(self._prompts().count(), 0)

    def test_prompt_is_sent_once_per_application(self):
        application = self._application()

        self._change_status(
            application, RecruitmentApplication.Status.SELECTED
        )
        self._change_status(
            application, RecruitmentApplication.Status.REJECTED
        )
        self._change_status(
            application, RecruitmentApplication.Status.SELECTED
        )

        self.assertEqual(self._prompts().count(), 1)


# =====================================================================
# HTTP WIRING
# =====================================================================

class CareerEntryAPITests(CareerEntryServiceTestCase, APITestCase):
    """
    A thin pass over the view layer — the rules are covered above, this only
    proves the URLs, serializers and the exception→envelope mapping line up.
    """

    def _auth(self, user, org=None):
        self.client.force_authenticate(user=user)
        if org is None:
            return {}
        return {
            "HTTP_X_ACTOR_TYPE": "organization",
            "HTTP_X_ACTOR_ID": str(org.id),
        }

    def test_create_list_patch_delete_round_trip(self):
        headers = self._auth(self.player)

        created = self.client.post(
            "/careers/create",
            {
                "sport": str(self.football.id),
                "organization": str(self.club.id),
                "title": "Player",
                "positions": [str(self.striker.id)],
                "start_date": "2020-01-01",
                "is_current": True,
            },
            format="json",
            **headers,
        )
        self.assertEqual(created.status_code, 201)

        body = created.data["data"]
        entry_id = body["id"]
        self.assertEqual(body["organization"]["username"], "dreamfc")
        self.assertEqual(body["organization_name"], "Dream FC")
        self.assertEqual(body["sport"]["name"], "Football")
        self.assertEqual(
            [p["name"] for p in body["positions"]], ["Striker"]
        )
        self.assertEqual(body["verification_status"], "pending")

        # Public read — an org actor sees the same history.
        listed = self.client.get(
            f"/careers/users/{self.player.id}",
            **self._auth(self.other, self._org_actor(self.other, self.club).organization),
        )
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.data["data"]["count"], 1)
        self.assertFalse(listed.data["data"]["is_owner"])

        patched = self.client.patch(
            f"/careers/{entry_id}",
            {"description": "Promoted twice."},
            format="json",
            **self._auth(self.player),
        )
        self.assertEqual(patched.status_code, 200)
        self.assertEqual(patched.data["data"]["description"], "Promoted twice.")

        removed = self.client.delete(
            f"/careers/{entry_id}", **self._auth(self.player)
        )
        self.assertEqual(removed.status_code, 200)
        self.assertFalse(CareerEntry.objects.filter(id=entry_id).exists())

    def test_org_actor_write_is_403_before_body_validation(self):
        # A *verified* member acting as the org — so the 403 comes from the
        # career rule, not from resolve_actor rejecting the membership.
        self._org_actor(self.player, self.club)

        response = self.client.post(
            "/careers/create",
            {"garbage": True},
            format="json",
            **self._auth(self.player, self.club),
        )

        self.assertEqual(response.status_code, 403)

    def test_patching_someone_elses_entry_is_403(self):
        entry = self._create()

        response = self.client.patch(
            f"/careers/{entry.id}",
            {"title": "Captain"},
            format="json",
            **self._auth(self.other),
        )

        self.assertEqual(response.status_code, 403)

    def test_bad_body_is_400_with_field_errors(self):
        response = self.client.post(
            "/careers/create",
            {"sport": str(self.football.id)},
            format="json",
            **self._auth(self.player),
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("title", response.data["data"]["errors"])

    def test_unknown_entry_is_404(self):
        response = self.client.delete(
            "/careers/00000000-0000-0000-0000-000000000001",
            **self._auth(self.player),
        )

        self.assertEqual(response.status_code, 404)


class CareerFromApplicationAPITests(CareerFromApplicationTestCase, APITestCase):
    """The 201-then-200 contract the client relies on to fire blind."""

    def _auth(self, user):
        self.client.force_authenticate(user=user)
        return {}

    def _post(self, application, body=None, user=None):
        return self.client.post(
            f"/careers/from-application/{application.id}",
            body or {},
            format="json",
            **self._auth(user or self.player),
        )

    def test_first_call_creates_second_call_returns(self):
        application = self._select(self._application(position=self.striker))

        created = self._post(application)
        self.assertEqual(created.status_code, 201)

        body = created.data["data"]
        self.assertEqual(body["organization"]["username"], "dreamfc")
        self.assertEqual(body["verification_status"], "verified")
        self.assertEqual(body["source"], "recruitment")
        self.assertEqual([p["name"] for p in body["positions"]], ["Striker"])

        repeated = self._post(application)
        self.assertEqual(repeated.status_code, 200)
        self.assertEqual(repeated.data["data"]["id"], body["id"])
        self.assertEqual(CareerEntry.objects.count(), 1)

    def test_not_selected_is_400(self):
        application = self._application()

        self.assertEqual(self._post(application).status_code, 400)

    def test_someone_elses_application_is_403(self):
        application = self._select(self._application())

        self.assertEqual(
            self._post(application, user=self.other).status_code, 403
        )


class CareerVerificationAPITests(CareerVerificationTestCase, APITestCase):
    """Thin pass over the three org-side endpoints."""

    def _auth(self, user, org=None):
        self.client.force_authenticate(user=user)
        if org is None:
            return {}
        return {
            "HTTP_X_ACTOR_TYPE": "organization",
            "HTTP_X_ACTOR_ID": str(org.id),
        }

    def test_queue_verify_round_trip(self):
        entry = self._pending_entry()

        queue = self.client.get(
            "/careers/verification-requests",
            **self._auth(self.club_owner, self.club),
        )
        self.assertEqual(queue.status_code, 200)
        self.assertEqual(queue.data["data"]["count"], 1)

        row = queue.data["data"]["results"][0]
        self.assertEqual(row["user"]["username"], "player")
        self.assertEqual(row["user"]["role"], User.Role.PLAYER)
        self.assertEqual(row["sport"]["name"], "Football")
        self.assertEqual(row["title"], "Player")

        with self.captureOnCommitCallbacks(execute=True):
            verified = self.client.post(
                f"/careers/{entry.id}/verify",
                {},
                format="json",
                **self._auth(self.club_owner, self.club),
            )

        self.assertEqual(verified.status_code, 200)
        self.assertEqual(verified.data["data"]["verification_status"], "verified")
        self.assertTrue(
            Notification.objects.filter(
                type=Notification.Type.CAREER_VERIFIED,
                recipient_user=self.player,
            ).exists()
        )

    def test_reject_with_reason(self):
        entry = self._pending_entry()

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                f"/careers/{entry.id}/reject",
                {"reason": "Not on our books."},
                format="json",
                **self._auth(self.club_owner, self.club),
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["data"]["verification_status"], "rejected")

        notification = Notification.objects.get(
            type=Notification.Type.CAREER_REJECTED
        )
        self.assertEqual(notification.data["reason"], "Not on our books.")

    def test_coach_gets_403_on_every_endpoint(self):
        entry = self._pending_entry()
        headers = self._auth(self.club_coach, self.club)

        self.assertEqual(
            self.client.get("/careers/verification-requests", **headers).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                f"/careers/{entry.id}/verify", {}, format="json", **headers
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                f"/careers/{entry.id}/reject", {}, format="json", **headers
            ).status_code,
            403,
        )

    def test_wrong_org_gets_403(self):
        entry = self._pending_entry()

        response = self.client.post(
            f"/careers/{entry.id}/verify",
            {},
            format="json",
            **self._auth(self.rival_owner, self.rival),
        )

        self.assertEqual(response.status_code, 403)

    def test_verifying_a_decided_entry_is_400(self):
        entry = self._pending_entry()
        CareerVerificationService.verify_entry(self.owner_actor, entry.id)

        response = self.client.post(
            f"/careers/{entry.id}/verify",
            {},
            format="json",
            **self._auth(self.club_owner, self.club),
        )

        self.assertEqual(response.status_code, 400)

    def test_queue_tabs_and_pagination(self):
        """Requests vs History, paginated, with an honest total."""
        pending = [self._pending_entry() for _ in range(3)]
        CareerVerificationService.verify_entry(self.owner_actor, pending[0].id)

        headers = self._auth(self.club_owner, self.club)

        requests = self.client.get(
            "/careers/verification-requests?status=pending&limit=1", **headers
        )
        self.assertEqual(requests.status_code, 200)
        self.assertEqual(requests.data["data"]["count"], 2)
        self.assertEqual(len(requests.data["data"]["results"]), 1)
        self.assertTrue(requests.data["data"]["has_more"])

        page2 = self.client.get(
            "/careers/verification-requests?status=pending&limit=1&offset=1",
            **headers,
        )
        self.assertEqual(len(page2.data["data"]["results"]), 1)
        self.assertFalse(page2.data["data"]["has_more"])
        self.assertNotEqual(
            page2.data["data"]["results"][0]["id"],
            requests.data["data"]["results"][0]["id"],
        )

        history = self.client.get(
            "/careers/verification-requests?status=decided", **headers
        )
        self.assertEqual(history.data["data"]["count"], 1)
        self.assertEqual(
            history.data["data"]["results"][0]["id"], str(pending[0].id)
        )

    def test_queue_rejects_an_unknown_status(self):
        response = self.client.get(
            "/careers/verification-requests?status=nonsense",
            **self._auth(self.club_owner, self.club),
        )

        self.assertEqual(response.status_code, 400)

    def test_decision_can_be_changed_from_history(self):
        entry = self._pending_entry()
        headers = self._auth(self.club_owner, self.club)

        self.client.post(f"/careers/{entry.id}/reject", {}, format="json", **headers)

        with self.captureOnCommitCallbacks(execute=True):
            flipped = self.client.post(
                f"/careers/{entry.id}/verify", {}, format="json", **headers
            )

        self.assertEqual(flipped.status_code, 200)
        self.assertEqual(
            flipped.data["data"]["verification_status"], "verified"
        )

    def test_user_actor_gets_403_on_the_queue(self):
        response = self.client.get(
            "/careers/verification-requests",
            **self._auth(self.player),
        )

        self.assertEqual(response.status_code, 403)


# =====================================================================
# STAGE 9 — PERMISSION MATRIX
# =====================================================================

class CareerPermissionMatrixTests(CareerVerificationTestCase):
    """
    One test per cell of the matrix the API enforces, including the
    combinations earlier stages only covered on a single endpoint.
    """

    def test_signed_out_actor_is_rejected_on_every_write(self):
        """`actor=None` is what resolve_actor yields for an anonymous request."""
        entry = self._create()

        with self.assertRaises(PermissionDenied):
            CareerEntryService.create_entry(None, payload=self._payload())

        with self.assertRaises(PermissionDenied):
            CareerEntryService.update_entry(None, entry.id, payload={"title": "X"})

        with self.assertRaises(PermissionDenied):
            CareerEntryService.delete_entry(None, entry.id)

        with self.assertRaises(PermissionDenied):
            CareerVerificationService.verify_entry(None, entry.id)

        with self.assertRaises(PermissionDenied):
            CareerVerificationService.reject_entry(None, entry.id)

    def test_org_actor_cannot_delete(self):
        """The gap earlier stages left: delete had no org-actor test."""
        entry = self._create()

        with self.assertRaises(PermissionDenied):
            CareerEntryService.delete_entry(self.owner_actor, entry.id)

        self.assertTrue(CareerEntry.objects.filter(id=entry.id).exists())

    def test_org_actor_without_membership_cannot_review(self):
        """
        An Actor built for an org the user isn't a member of. resolve_actor
        refuses it upstream, but the service must not depend on that.
        """
        rogue = Actor(
            actor_type="organization",
            organization=self.club,
            organization_member=None,
        )
        entry = self._pending_entry()

        with self.assertRaises(PermissionDenied):
            CareerVerificationService.verify_entry(rogue, entry.id)

    def test_coach_and_staff_are_refused_both_decisions(self):
        """QA #8 — both roles, both actions."""
        for actor in (self.coach_actor, self.staff_actor):
            entry = self._pending_entry()

            with self.assertRaises(PermissionDenied):
                CareerVerificationService.verify_entry(actor, entry.id)
            with self.assertRaises(PermissionDenied):
                CareerVerificationService.reject_entry(actor, entry.id)

            entry.refresh_from_db()
            self.assertEqual(
                entry.verification_status,
                CareerEntry.VerificationStatus.PENDING
            )

    def test_coach_and_staff_cannot_read_the_queue(self):
        """The role gate covers the queue, not only the decisions."""
        for actor in (self.coach_actor, self.staff_actor):
            with self.assertRaises(PermissionDenied):
                CareerVerificationService.require_reviewer(actor)

    def test_from_application_rejects_org_actor_and_wrong_user(self):
        recruitment = Recruitment.objects.create(
            organization=self.club,
            sport=self.football,
            title="Trial",
            recruitment_type=Recruitment.Type.OPEN_TRIAL,
            status=Recruitment.Status.ACTIVE,
        )
        application = RecruitmentApplication.objects.create(
            recruitment=recruitment,
            applicant=self.player,
            shared_name="Player",
            shared_phone="9999999999",
            status=RecruitmentApplication.Status.SELECTED,
        )

        with self.assertRaises(PermissionDenied):
            CareerEntryService.create_from_application(
                self.owner_actor, application.id
            )

        with self.assertRaises(PermissionDenied):
            CareerEntryService.create_from_application(
                Actor(actor_type="user", user=self.other), application.id
            )


# =====================================================================
# STAGE 9 — QUERY BUDGETS (N+1 guards)
# =====================================================================

class CareerQueryBudgetTests(CareerVerificationTestCase):
    """
    A dropped select_related shows up as a query count that GROWS with the row
    count, so that — not an absolute number — is what these assert. They render
    through the real serializers, because the joins only pay off if they cover
    the fields the response actually reads.
    """

    def _entries(self, count):
        for index in range(count):
            self._create(
                title=f"role {index}",
                organization=self.club.id,
                positions=[self.striker.id],
            )

    def _count_queries(self, render):
        with CaptureQueriesContext(connection) as ctx:
            render()
        return len(ctx.captured_queries)

    def _assert_constant(self, render):
        """Same query count for one row as for five."""
        self._entries(1)
        one = self._count_queries(render)

        CareerEntry.objects.all().delete()

        self._entries(5)
        five = self._count_queries(render)

        self.assertEqual(
            one,
            five,
            f"query count grew with rows ({one} → {five}) — a join is missing",
        )
        # Sanity ceiling: the page itself, plus the positions prefetch.
        self.assertLessEqual(five, 3)

    def test_career_list_is_constant_query_count(self):
        self._assert_constant(
            lambda: CareerEntrySerializer(
                list(career_entries_for(self.player)), many=True
            ).data
        )

    def test_verification_queue_is_constant_query_count(self):
        self._assert_constant(
            lambda: CareerVerificationRequestSerializer(
                list(pending_verification_requests_for(self.club)), many=True
            ).data
        )

    def test_decision_loads_everything_the_response_needs(self):
        """
        verify() must return an entry the serializer can render without going
        back to the database for the org logo, the sport or the positions.
        """
        entry = self._create(
            organization=self.club.id, positions=[self.striker.id]
        )

        verified = CareerVerificationService.verify_entry(
            self.owner_actor, entry.id
        )

        with self.assertNumQueries(0):
            CareerEntrySerializer(verified).data


# =====================================================================
# STAGE 9 — QA CHECKLIST
# =====================================================================

class CareerQAChecklistTests(CareerVerificationTestCase):
    """One test per manual QA item that wasn't already covered end to end."""

    def test_qa4_date_edit_on_verified_entry_re_requests(self):
        """QA #4 — the checklist names DATES specifically, not just any field."""
        entry = self._pending_entry()
        CareerVerificationService.verify_entry(self.owner_actor, entry.id)

        requests_before = Notification.objects.filter(
            type=Notification.Type.CAREER_VERIFICATION_REQUEST
        ).count()

        with self.captureOnCommitCallbacks(execute=True):
            updated = CareerEntryService.update_entry(
                self.actor,
                entry.id,
                payload={"start_date": date(2019, 6, 1)},
            )

        self.assertEqual(
            updated.verification_status,
            CareerEntry.VerificationStatus.PENDING
        )
        self.assertEqual(
            Notification.objects.filter(
                type=Notification.Type.CAREER_VERIFICATION_REQUEST
            ).count(),
            requests_before + 1,
        )

    def test_qa5_overlapping_entries_all_survive_and_order_correctly(self):
        """
        QA #5 — a club spell with a loan inside it and a national team call-up
        running alongside. Nothing rejects the overlap, and current sorts first.
        """
        self._create(
            title="Club",
            start_date=date(2019, 1, 1),
            is_current=True,
        )
        self._create(
            title="Loan",
            entry_type=CareerEntry.EntryType.LOAN,
            start_date=date(2020, 1, 1),
            end_date=date(2020, 12, 1),
        )
        self._create(
            title="National Team",
            entry_type=CareerEntry.EntryType.NATIONAL_TEAM,
            start_date=date(2021, 1, 1),
            end_date=date(2022, 1, 1),
        )

        entries = list(career_entries_for(self.player))

        self.assertEqual(len(entries), 3)
        # Current first, then most recently ended.
        self.assertEqual(
            [e.title for e in entries],
            ["Club", "National Team", "Loan"],
        )

    def test_qa7_coach_and_scout_can_manage_their_own_career(self):
        """
        QA #7 — careers are NOT players-only (unlike highlights). A coach's
        "Head Coach at X" is exactly what this feature is for.
        """
        for username, role in (("acoach", User.Role.COACH), ("ascout", User.Role.SCOUT)):
            user = self._user(username, role)
            actor = Actor(actor_type="user", user=user)

            entry = CareerEntryService.create_entry(
                actor,
                payload=self._payload(title="Head Coach"),
            )

            self.assertEqual(entry.user_id, user.id)
            self.assertEqual(entry.title, "Head Coach")

            updated = CareerEntryService.update_entry(
                actor, entry.id, payload={"title": "Assistant Coach"}
            )
            self.assertEqual(updated.title, "Assistant Coach")

            CareerEntryService.delete_entry(actor, entry.id)
            self.assertFalse(CareerEntry.objects.filter(id=entry.id).exists())


# =====================================================================
# STAGE 9 — NOTIFICATION GROUPING
# =====================================================================

class CareerNotificationGroupingTests(CareerVerificationTestCase):
    """
    The career types have three different grouping shapes, and each of them can
    fail in a different way:

      career_verification_request → grouped per ORG (many players → one row)
      career_verified / rejected  → never grouped (each decision is its own)
      career_add_prompt           → deduplicated per application
    """

    def _grouped(self, recipient_org=None, recipient_user=None):
        rows = (
            Notification.objects
            .filter(
                recipient_org=recipient_org,
                recipient_user=recipient_user,
                is_deleted=False,
            )
            .select_related(
                "actor_user__profile",
                "actor_org__profile",
                "post",
                "comment",
                "recruitment",
                "career_entry",
            )
            .order_by("-created_at")
        )
        return NotificationGroupingService.group_notifications(list(rows))

    def test_two_requests_from_one_player_list_them_once(self):
        """
        The bug this guards: actors were collected per ROW, so one player with
        two pending entries at the same club rendered as "Alice, Alice listed
        you on their career".
        """
        with self.captureOnCommitCallbacks(execute=True):
            self._create(title="Player", organization=self.club.id)
            self._create(title="Captain", organization=self.club.id)

        groups = self._grouped(recipient_org=self.club)

        self.assertEqual(len(groups), 1)
        group = groups[0]
        self.assertEqual(len(group["actors"]), 1)
        self.assertEqual(group["others_count"], 0)
        self.assertNotIn(", ", group["text"].split(" listed")[0])

    def test_requests_from_two_players_group_with_both_actors(self):
        with self.captureOnCommitCallbacks(execute=True):
            self._create(organization=self.club.id)
            CareerEntryService.create_entry(
                Actor(actor_type="user", user=self.other),
                payload=self._payload(organization=self.club.id),
            )

        groups = self._grouped(recipient_org=self.club)

        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]["actors"]), 2)

    def test_decisions_never_collapse_into_one_row(self):
        """
        A club that verifies one entry and rejects another must leave the
        player two separate rows — they say opposite things.
        """
        first = self._pending_entry()
        second = self._create(title="Captain", organization=self.club.id)

        with self.captureOnCommitCallbacks(execute=True):
            CareerVerificationService.verify_entry(self.owner_actor, first.id)
            CareerVerificationService.reject_entry(
                self.owner_actor, second.id, reason="Not our squad"
            )

        groups = self._grouped(recipient_user=self.player)
        types = {group["type"] for group in groups}

        self.assertEqual(len(groups), 2)
        self.assertEqual(
            types,
            {
                Notification.Type.CAREER_VERIFIED,
                Notification.Type.CAREER_REJECTED,
            },
        )

    def test_two_verifications_stay_two_rows(self):
        """Same type, same actor — still two decisions, so still two rows."""
        first = self._pending_entry()
        second = self._create(title="Captain", organization=self.club.id)

        with self.captureOnCommitCallbacks(execute=True):
            CareerVerificationService.verify_entry(self.owner_actor, first.id)
            CareerVerificationService.verify_entry(self.owner_actor, second.id)

        groups = self._grouped(recipient_user=self.player)

        self.assertEqual(len(groups), 2)

    def test_grouped_response_exposes_the_data_payload(self):
        """
        career_add_prompt carries its application_id ONLY in `data`, so the
        client cannot act on the prompt if the grouping drops it.
        """
        recruitment = Recruitment.objects.create(
            organization=self.club,
            sport=self.football,
            title="U19 Trial",
            recruitment_type=Recruitment.Type.OPEN_TRIAL,
            status=Recruitment.Status.ACTIVE,
        )
        application = RecruitmentApplication.objects.create(
            recruitment=recruitment,
            applicant=self.player,
            shared_name="Player",
            shared_phone="9999999999",
        )

        with self.captureOnCommitCallbacks(execute=True):
            ApplicationService.change_status(
                self.owner_actor,
                recruitment,
                [str(application.id)],
                RecruitmentApplication.Status.SELECTED,
            )

        groups = self._grouped(recipient_user=self.player)
        prompt = next(
            g for g in groups
            if g["type"] == Notification.Type.CAREER_ADD_PROMPT
        )

        self.assertEqual(
            prompt["data"]["application_id"], str(application.id)
        )

    def test_career_entry_block_is_attached(self):
        """The grouped row carries enough to render without a second fetch."""
        with self.captureOnCommitCallbacks(execute=True):
            entry = self._create(organization=self.club.id)

        group = self._grouped(recipient_org=self.club)[0]

        self.assertEqual(group["career_entry"]["id"], str(entry.id))
        self.assertEqual(group["career_entry"]["verification_status"], "pending")
