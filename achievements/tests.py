"""
Service-level tests for achievements.

These call AchievementService / AchievementVerificationService / the selectors
directly rather than going through the API, because that is where the rules live
— the views only translate exceptions into the response envelope. The services
take a ``core.actor.Actor``, so the tests build one the same way
``resolve_actor`` would.
"""

from datetime import date, timedelta

from django.db import IntegrityError, connection, transaction
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
from achievements.models import Achievement
from achievements.selectors.achievement_selectors import (
    count_for_user,
    decided_verification_requests_for,
    get_by_id,
    list_for_user,
    pending_verification_requests_for,
)
from achievements.serializers.achievement_serializers import (
    AchievementSerializer,
    AchievementVerificationRequestSerializer,
)
from achievements.services.achievement_services import AchievementService
from achievements.services.achievement_verification_services import (
    AchievementVerificationService,
)
from careers.models import CareerEntry
from core.actor import Actor
from notifications.models import Notification
from notifications.services.grouping_service import NotificationGroupingService
from notifications.services.notification_service import (
    build_notification_payload,
)
from organization.models import (
    Organization,
    OrganizationMember,
    OrganizationProfile,
)
from sports.models import Sport


@override_settings(
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"]
)
class AchievementServiceTestCase(TestCase):
    """Shared cast: one owner, one other user, two sports, one issuing org."""

    def setUp(self):
        self.player = self._user("player")
        self.other = self._user("other")

        self.football = Sport.objects.create(name="Football", icon_name="mdi:soccer")
        self.basketball = Sport.objects.create(name="Basketball", icon_name="mdi:basketball")

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

    def _career_entry(self, user=None, sport=None):
        return CareerEntry.objects.create(
            user=user or self.player,
            organization_name="Dream FC",
            sport=sport or self.football,
            title="Player",
            start_date=date(2020, 1, 1),
        )

    def _payload(self, **overrides):
        payload = {
            "sport": self.football.id,
            "title": "Golden Boot",
            "achieved_date": date(2024, 5, 1),
        }
        payload.update(overrides)
        return payload

    def _create(self, **overrides):
        return AchievementService.create_achievement(
            self.actor,
            payload=self._payload(**overrides)
        )

    def _verify(self, achievement, by=None):
        """
        Put an award into the state an org confirmation would leave it in.

        Only valid on a row that credits an org — the DB constraint refuses a
        verified row with no issuer, which AchievementModelTests covers directly.
        """
        self.assertIsNotNone(
            achievement.awarded_by_id,
            "_verify needs an award that credits an org",
        )
        achievement.verification_status = Achievement.VerificationStatus.VERIFIED
        achievement.verified_by = by or self.other
        achievement.verified_at = timezone.now()
        achievement.save()
        return achievement


# =====================================================================
# MODEL & SCHEMA
# =====================================================================

class AchievementModelTests(AchievementServiceTestCase):
    """
    The guarantees the database itself makes, independent of any service. These
    poke the ORM directly on purpose — the services are careful, and the point
    here is what happens when something else is not.
    """

    def _row(self, **overrides):
        fields = {
            "user": self.player,
            "title": "Golden Boot",
            "sport": self.football,
            "achieved_date": date(2024, 5, 1),
        }
        fields.update(overrides)
        return Achievement(**fields)

    def test_issuerless_row_cannot_be_pending_verified_or_rejected(self):
        """
        A verification state names a decision somebody owes or has made. With no
        issuer there is nobody to ask, so the row can only be self_reported.
        """
        for status in (
            Achievement.VerificationStatus.PENDING,
            Achievement.VerificationStatus.VERIFIED,
            Achievement.VerificationStatus.REJECTED,
        ):
            with self.subTest(status=status):
                with self.assertRaises(IntegrityError):
                    with transaction.atomic():
                        self._row(verification_status=status).save()

    def test_issuerless_row_may_be_self_reported(self):
        row = self._row(
            verification_status=Achievement.VerificationStatus.SELF_REPORTED
        )
        row.save()
        self.assertIsNone(row.awarded_by_id)

    def test_row_with_an_issuer_may_hold_any_status(self):
        for status in Achievement.VerificationStatus.values:
            with self.subTest(status=status):
                row = self._row(awarded_by=self.club, verification_status=status)
                row.save()
                self.assertEqual(row.verification_status, status)
                row.delete()

    def test_awarded_by_name_is_synced_from_the_linked_org(self):
        """The client's free text loses to the org's real name."""
        row = self._row(awarded_by=self.club, awarded_by_name="whatever I typed")
        row.save()

        self.assertEqual(row.awarded_by_name, "Dream FC")

    def test_awarded_by_name_is_clipped_to_the_column_width(self):
        """
        Organization.name allows 255, this column 150 — a long federation name
        is clipped rather than blowing up the insert.
        """
        long_name = "A" * 200
        federation = Organization.objects.create(
            name=long_name,
            username="longfed",
            type=Organization.Type.CLUB,
        )

        row = self._row(awarded_by=federation)
        row.save()

        self.assertEqual(len(row.awarded_by_name), 150)
        self.assertEqual(row.awarded_by_name, long_name[:150])

    def test_free_text_issuer_survives_when_there_is_no_link(self):
        """Unlike a career entry, the name stands alone for an off-platform body."""
        row = self._row(awarded_by_name="Kerala Football Association")
        row.save()

        self.assertIsNone(row.awarded_by_id)
        self.assertEqual(row.awarded_by_name, "Kerala Football Association")

    def test_issuer_name_survives_the_org_being_deleted(self):
        """
        The whole reason the column is denormalized. The FK goes null through a
        bulk update that never reaches save(), so the last synced name is what
        is left — and it must still be there.
        """
        row = self._row(awarded_by=self.club)
        row.save()

        self.club.delete()
        row.refresh_from_db()

        self.assertIsNone(row.awarded_by_id)
        self.assertEqual(row.awarded_by_name, "Dream FC")

    def test_deleting_an_issuer_releases_its_decided_awards(self):
        """
        Regression: SET_NULL is a bulk UPDATE, so a decided row would be left
        with no issuer and trip the pairing constraint — taking the org delete
        down with it. The pre_delete hook resets those rows first.
        """
        pending = self._row(
            awarded_by=self.club,
            verification_status=Achievement.VerificationStatus.PENDING,
        )
        pending.save()

        verified = self._row(
            title="Player of the Season",
            awarded_by=self.club,
            verification_status=Achievement.VerificationStatus.VERIFIED,
            verified_by=self.other,
            verified_at=timezone.now(),
        )
        verified.save()

        self.club.delete()

        for row in (pending, verified):
            row.refresh_from_db()
            self.assertIsNone(row.awarded_by_id)
            self.assertEqual(
                row.verification_status,
                Achievement.VerificationStatus.SELF_REPORTED
            )
            # A self-reported row carrying a verifier would be a lie.
            self.assertIsNone(row.verified_by_id)
            self.assertIsNone(row.verified_at)
            self.assertEqual(row.awarded_by_name, "Dream FC")

    def test_deleting_an_issuer_leaves_other_orgs_alone(self):
        rival = self._org("rivalfc", "Rival FC")
        theirs = self._row(
            awarded_by=rival,
            verification_status=Achievement.VerificationStatus.PENDING,
        )
        theirs.save()

        self.club.delete()
        theirs.refresh_from_db()

        self.assertEqual(theirs.awarded_by_id, rival.id)
        self.assertEqual(
            theirs.verification_status,
            Achievement.VerificationStatus.PENDING
        )

    def test_default_ordering_is_pinned_then_newest_achieved(self):
        self._row(title="old", achieved_date=date(2020, 1, 1)).save()
        self._row(title="new", achieved_date=date(2024, 1, 1)).save()
        self._row(
            title="pinned but ancient",
            achieved_date=date(2015, 1, 1),
            is_pinned=True,
        ).save()

        self.assertEqual(
            [a.title for a in Achievement.objects.all()],
            ["pinned but ancient", "new", "old"],
        )

    def test_created_at_breaks_a_same_date_tie(self):
        """A cup final hands out two awards on one day; the later add sorts first."""
        first = self._row(title="Trophy", achieved_date=date(2024, 5, 1))
        first.save()
        second = self._row(title="Man of the Match", achieved_date=date(2024, 5, 1))
        second.save()

        self.assertEqual(
            [a.title for a in Achievement.objects.all()],
            ["Man of the Match", "Trophy"],
        )

    def test_str_names_the_award_and_its_owner(self):
        row = self._row()
        row.save()
        self.assertIn("Golden Boot", str(row))


# =====================================================================
# CREATE
# =====================================================================

class CreateAchievementTests(AchievementServiceTestCase):

    def test_minimal_payload(self):
        """Sport, title and a date are the whole requirement."""
        achievement = self._create()

        self.assertEqual(achievement.title, "Golden Boot")
        self.assertEqual(achievement.sport_id, self.football.id)
        self.assertEqual(achievement.achieved_date, date(2024, 5, 1))
        # Model default, applied when the client says nothing.
        self.assertEqual(
            achievement.achievement_type,
            Achievement.AchievementType.INDIVIDUAL_AWARD
        )
        self.assertEqual(achievement.awarded_by_name, "")
        self.assertEqual(achievement.level, "")
        self.assertFalse(achievement.is_pinned)
        self.assertEqual(
            achievement.verification_status,
            Achievement.VerificationStatus.SELF_REPORTED
        )

    def test_full_payload(self):
        entry = self._career_entry()

        achievement = self._create(
            title="  Player of the Season  ",
            achievement_type=Achievement.AchievementType.TEAM_TROPHY,
            description="  Unbeaten run.  ",
            event_name="  Kerala Premier League 2024  ",
            level=Achievement.Level.STATE,
            awarded_by=self.club.id,
            career_entry=entry.id,
            image="https://res.cloudinary.com/demo/image/upload/v1/cert.jpg",
            image_public_id="achievements/abc/def",
            reference_link="https://example.com/report",
            is_pinned=True,
        )

        # Everything free-text is trimmed on the way in.
        self.assertEqual(achievement.title, "Player of the Season")
        self.assertEqual(achievement.description, "Unbeaten run.")
        self.assertEqual(achievement.event_name, "Kerala Premier League 2024")
        self.assertEqual(
            achievement.achievement_type,
            Achievement.AchievementType.TEAM_TROPHY
        )
        self.assertEqual(achievement.level, Achievement.Level.STATE)
        self.assertEqual(achievement.awarded_by_id, self.club.id)
        self.assertEqual(achievement.career_entry_id, entry.id)
        self.assertEqual(achievement.image_public_id, "achievements/abc/def")
        self.assertEqual(achievement.reference_link, "https://example.com/report")
        self.assertTrue(achievement.is_pinned)

    def test_create_with_issuer_is_pending(self):
        """A claim against a real org needs that org's confirmation."""
        achievement = self._create(awarded_by=self.club.id)

        self.assertEqual(
            achievement.verification_status,
            Achievement.VerificationStatus.PENDING
        )
        self.assertEqual(achievement.awarded_by_id, self.club.id)
        # Synced from the org, not from whatever the client typed.
        self.assertEqual(achievement.awarded_by_name, "Dream FC")
        self.assertIsNone(achievement.verified_by_id)
        self.assertIsNone(achievement.verified_at)

    def test_create_with_free_text_issuer_is_self_reported(self):
        """Nobody on the platform can confirm a federation that is not on it."""
        achievement = self._create(awarded_by_name="Kerala Football Association")

        self.assertEqual(
            achievement.verification_status,
            Achievement.VerificationStatus.SELF_REPORTED
        )
        self.assertIsNone(achievement.awarded_by_id)
        self.assertEqual(achievement.awarded_by_name, "Kerala Football Association")

    def test_create_with_no_issuer_at_all_is_allowed(self):
        """
        The deliberate divergence from careers: plenty of awards have nobody
        who issued them, so there is no "pick one or type one" rule.
        """
        achievement = self._create()

        self.assertIsNone(achievement.awarded_by_id)
        self.assertEqual(achievement.awarded_by_name, "")

    def test_verification_status_is_never_client_settable(self):
        """It is derived from the issuer, and nobody verifies themselves."""
        achievement = self._create(
            verification_status=Achievement.VerificationStatus.VERIFIED
        )

        self.assertEqual(
            achievement.verification_status,
            Achievement.VerificationStatus.SELF_REPORTED
        )

    def test_future_achieved_date_is_rejected(self):
        """No DB constraint can hold this — CheckConstraints cannot call now()."""
        with self.assertRaises(ValidationError) as ctx:
            self._create(achieved_date=timezone.localdate() + timedelta(days=1))

        self.assertIn("future", str(ctx.exception.detail).lower())
        self.assertEqual(Achievement.objects.count(), 0)

    def test_today_is_allowed(self):
        """The boundary: you can win something this morning."""
        achievement = self._create(achieved_date=timezone.localdate())

        self.assertEqual(achievement.achieved_date, timezone.localdate())

    def test_missing_achieved_date_is_rejected(self):
        with self.assertRaises(ValidationError):
            self._create(achieved_date=None)

    def test_blank_title_is_rejected(self):
        with self.assertRaises(ValidationError):
            self._create(title="   ")

    def test_unknown_sport_is_rejected(self):
        with self.assertRaises(ValidationError):
            self._create(sport="00000000-0000-0000-0000-000000000001")

        with self.assertRaises(ValidationError):
            self._create(sport=None)

    def test_unknown_organization_is_rejected(self):
        with self.assertRaises(ValidationError) as ctx:
            self._create(awarded_by="00000000-0000-0000-0000-000000000001")

        self.assertIn("no longer on Goatza", str(ctx.exception.detail))
        self.assertEqual(Achievement.objects.count(), 0)

    def test_inactive_organization_is_rejected(self):
        self.club.is_active = False
        self.club.save()

        with self.assertRaises(ValidationError):
            self._create(awarded_by=self.club.id)

    def test_overlong_reference_link_is_a_clean_400(self):
        """
        The column is URLField's default 200. Catching it here is the difference
        between a 400 and an insert that only fails in production.
        """
        with self.assertRaises(ValidationError):
            self._create(reference_link="https://e.com/" + "x" * 250)

    def test_long_cloudinary_image_url_fits(self):
        """`image` was widened to 500 for exactly this shape of URL."""
        url = "https://res.cloudinary.com/demo/image/upload/v1/" + "a" * 300

        achievement = self._create(image=url)

        self.assertEqual(achievement.image, url)

    def test_organization_actor_cannot_create(self):
        """Achievements belong to a person; an org actor is refused outright."""
        org_actor = self._org_actor(self.player, self.club)

        with self.assertRaises(PermissionDenied):
            AchievementService.create_achievement(
                org_actor, payload=self._payload()
            )

        self.assertEqual(Achievement.objects.count(), 0)


# =====================================================================
# CAPS
# =====================================================================

class AchievementCapTests(AchievementServiceTestCase):

    def test_achievements_are_capped(self):
        """A trophy shelf, not a results archive — MAX_ACHIEVEMENTS bounds it."""
        for index in range(AchievementService.MAX_ACHIEVEMENTS):
            self._create(title=f"award {index}")

        with self.assertRaises(ValidationError) as ctx:
            self._create(title="one too many")

        self.assertIn("20", str(ctx.exception.detail))
        self.assertEqual(
            Achievement.objects.filter(user=self.player).count(),
            AchievementService.MAX_ACHIEVEMENTS,
        )

    def test_the_cap_is_per_user(self):
        for index in range(AchievementService.MAX_ACHIEVEMENTS):
            self._create(title=f"award {index}")

        # Someone else is unaffected.
        theirs = AchievementService.create_achievement(
            Actor(actor_type="user", user=self.other),
            payload=self._payload(),
        )
        self.assertIsNotNone(theirs.id)

    def test_deleting_frees_a_slot(self):
        awards = [
            self._create(title=f"award {i}")
            for i in range(AchievementService.MAX_ACHIEVEMENTS)
        ]

        AchievementService.delete_achievement(self.actor, awards[0].id)

        self._create(title="replacement")
        self.assertEqual(
            Achievement.objects.filter(user=self.player).count(),
            AchievementService.MAX_ACHIEVEMENTS,
        )

    def test_pins_are_capped_on_create(self):
        for index in range(AchievementService.MAX_PINNED):
            self._create(title=f"pinned {index}", is_pinned=True)

        with self.assertRaises(ValidationError) as ctx:
            self._create(title="fourth pin", is_pinned=True)

        self.assertIn("3", str(ctx.exception.detail))
        # The whole create failed — no unpinned leftover row.
        self.assertEqual(
            Achievement.objects.filter(user=self.player).count(),
            AchievementService.MAX_PINNED,
        )

    def test_pins_are_capped_on_update(self):
        for index in range(AchievementService.MAX_PINNED):
            self._create(title=f"pinned {index}", is_pinned=True)
        spare = self._create(title="spare")

        with self.assertRaises(ValidationError):
            AchievementService.update_achievement(
                self.actor, spare.id, payload={"is_pinned": True}
            )

        spare.refresh_from_db()
        self.assertFalse(spare.is_pinned)

    def test_unpinning_frees_a_pin_slot(self):
        pins = [
            self._create(title=f"pinned {i}", is_pinned=True)
            for i in range(AchievementService.MAX_PINNED)
        ]
        spare = self._create(title="spare")

        AchievementService.update_achievement(
            self.actor, pins[0].id, payload={"is_pinned": False}
        )
        updated = AchievementService.update_achievement(
            self.actor, spare.id, payload={"is_pinned": True}
        )

        self.assertTrue(updated.is_pinned)
        self.assertEqual(
            Achievement.objects.filter(user=self.player, is_pinned=True).count(),
            AchievementService.MAX_PINNED,
        )

    def test_repinning_an_already_pinned_award_is_a_noop(self):
        """The cap counts the user's OTHER pins, so this must not trip itself."""
        pins = [
            self._create(title=f"pinned {i}", is_pinned=True)
            for i in range(AchievementService.MAX_PINNED)
        ]

        updated = AchievementService.update_achievement(
            self.actor, pins[0].id, payload={"is_pinned": True}
        )

        self.assertTrue(updated.is_pinned)

    def test_the_pin_cap_is_per_user(self):
        for index in range(AchievementService.MAX_PINNED):
            self._create(title=f"pinned {index}", is_pinned=True)

        theirs = AchievementService.create_achievement(
            Actor(actor_type="user", user=self.other),
            payload=self._payload(is_pinned=True),
        )
        self.assertTrue(theirs.is_pinned)


# =====================================================================
# CAREER ENTRY LINK
# =====================================================================

class AchievementCareerLinkTests(AchievementServiceTestCase):
    """
    The cross-reference to the career section. Optional, but when present it has
    to be coherent: your own entry, and the same sport.
    """

    def test_linking_your_own_matching_entry(self):
        entry = self._career_entry()

        achievement = self._create(career_entry=entry.id)

        self.assertEqual(achievement.career_entry_id, entry.id)

    def test_linking_someone_elses_entry_is_forbidden(self):
        """403, not 404 — hanging medals off a stranger's career is a refusal."""
        entry = self._career_entry(user=self.other)

        with self.assertRaises(PermissionDenied):
            self._create(career_entry=entry.id)

        self.assertEqual(Achievement.objects.count(), 0)

    def test_linking_an_entry_for_another_sport_is_rejected(self):
        """A basketball stint cannot hold a football award."""
        entry = self._career_entry(sport=self.basketball)

        with self.assertRaises(ValidationError) as ctx:
            self._create(career_entry=entry.id)

        self.assertIn("Basketball", str(ctx.exception.detail))
        self.assertEqual(Achievement.objects.count(), 0)

    def test_unknown_entry_is_rejected(self):
        with self.assertRaises(ValidationError):
            self._create(career_entry="00000000-0000-0000-0000-000000000001")

    def test_unlinking_on_update(self):
        entry = self._career_entry()
        achievement = self._create(career_entry=entry.id)

        updated = AchievementService.update_achievement(
            self.actor, achievement.id, payload={"career_entry": None}
        )

        self.assertIsNone(updated.career_entry_id)

    def test_changing_sport_that_orphans_the_link_is_rejected(self):
        """
        The deliberate divergence from careers, which silently clears stale
        positions. A career entry is a row the owner deliberately pointed at, so
        the disagreement is surfaced rather than resolved for them.
        """
        entry = self._career_entry()
        achievement = self._create(career_entry=entry.id)

        with self.assertRaises(ValidationError):
            AchievementService.update_achievement(
                self.actor,
                achievement.id,
                payload={"sport": self.basketball.id},
            )

        achievement.refresh_from_db()
        self.assertEqual(achievement.sport_id, self.football.id)
        self.assertEqual(achievement.career_entry_id, entry.id)

    def test_changing_sport_and_the_link_together_is_allowed(self):
        entry = self._career_entry()
        achievement = self._create(career_entry=entry.id)
        basketball_entry = self._career_entry(sport=self.basketball)

        updated = AchievementService.update_achievement(
            self.actor,
            achievement.id,
            payload={
                "sport": self.basketball.id,
                "career_entry": basketball_entry.id,
            },
        )

        self.assertEqual(updated.sport_id, self.basketball.id)
        self.assertEqual(updated.career_entry_id, basketball_entry.id)

    def test_changing_sport_with_no_link_is_fine(self):
        achievement = self._create()

        updated = AchievementService.update_achievement(
            self.actor, achievement.id, payload={"sport": self.basketball.id}
        )

        self.assertEqual(updated.sport_id, self.basketball.id)

    def test_deleting_the_entry_leaves_the_achievement(self):
        """SET_NULL — the award outlives the stint it was won during."""
        entry = self._career_entry()
        achievement = self._create(career_entry=entry.id)

        entry.delete()
        achievement.refresh_from_db()

        self.assertIsNone(achievement.career_entry_id)
        self.assertTrue(Achievement.objects.filter(id=achievement.id).exists())


# =====================================================================
# UPDATE
# =====================================================================

class UpdateAchievementTests(AchievementServiceTestCase):

    def _verified(self, **overrides):
        return self._verify(self._create(awarded_by=self.club.id, **overrides))

    def test_material_edit_on_verified_resets_to_pending(self):
        achievement = self._verified()

        updated = AchievementService.update_achievement(
            self.actor, achievement.id, payload={"title": "Golden Glove"}
        )

        self.assertEqual(updated.title, "Golden Glove")
        self.assertEqual(
            updated.verification_status,
            Achievement.VerificationStatus.PENDING
        )
        self.assertIsNone(updated.verified_by_id)
        self.assertIsNone(updated.verified_at)

    def test_every_material_field_resets_a_verified_award(self):
        """One subtest per MATERIAL_FIELDS entry the client can actually send."""
        cases = {
            "title": "Golden Glove",
            "achievement_type": Achievement.AchievementType.RECORD,
            "event_name": "Some Other Cup",
            "level": Achievement.Level.NATIONAL,
            "achieved_date": date(2023, 3, 3),
            "image": "https://res.cloudinary.com/demo/image/upload/v1/x.jpg",
        }

        for field, value in cases.items():
            with self.subTest(field=field):
                achievement = self._verified(title=f"Award {field}")

                updated = AchievementService.update_achievement(
                    self.actor, achievement.id, payload={field: value}
                )

                self.assertEqual(
                    updated.verification_status,
                    Achievement.VerificationStatus.PENDING,
                    f"{field} should be material",
                )

    def test_non_material_edits_keep_a_verified_award_verified(self):
        """
        description, reference_link, career_entry and is_pinned are the owner's
        commentary, sourcing, cross-linking and display choice — none of them
        changes what was claimed.
        """
        entry = self._career_entry()
        cases = {
            "description": "Top scorer with 19 goals.",
            "reference_link": "https://example.com/match-report",
            "career_entry": entry.id,
            "is_pinned": True,
        }

        for field, value in cases.items():
            with self.subTest(field=field):
                achievement = self._verified(title=f"Award {field}")
                verified_at = achievement.verified_at

                updated = AchievementService.update_achievement(
                    self.actor, achievement.id, payload={field: value}
                )

                self.assertEqual(
                    updated.verification_status,
                    Achievement.VerificationStatus.VERIFIED,
                    f"{field} should NOT be material",
                )
                self.assertEqual(updated.verified_by_id, self.other.id)
                self.assertEqual(updated.verified_at, verified_at)

    def test_image_public_id_alone_is_not_material(self):
        """It is the storage handle for the same upload, not a separate claim."""
        achievement = self._verified()

        updated = AchievementService.update_achievement(
            self.actor,
            achievement.id,
            payload={"image_public_id": "achievements/new/handle"},
        )

        self.assertEqual(updated.image_public_id, "achievements/new/handle")
        self.assertEqual(
            updated.verification_status,
            Achievement.VerificationStatus.VERIFIED
        )

    def test_resending_the_same_values_keeps_verified(self):
        """A client PATCHing the whole form back has not changed anything."""
        achievement = self._verified()

        updated = AchievementService.update_achievement(
            self.actor,
            achievement.id,
            payload={
                "title": achievement.title,
                "sport": achievement.sport_id,
                "achieved_date": achievement.achieved_date,
                "achievement_type": achievement.achievement_type,
            },
        )

        self.assertEqual(
            updated.verification_status,
            Achievement.VerificationStatus.VERIFIED
        )

    def test_linking_an_organization_moves_to_pending(self):
        achievement = self._create(awarded_by_name="Kerala FA")

        updated = AchievementService.update_achievement(
            self.actor, achievement.id, payload={"awarded_by": self.club.id}
        )

        self.assertEqual(
            updated.verification_status,
            Achievement.VerificationStatus.PENDING
        )
        self.assertEqual(updated.awarded_by_name, "Dream FC")

    def test_unlinking_an_organization_falls_back_to_self_reported(self):
        achievement = self._verified()

        updated = AchievementService.update_achievement(
            self.actor,
            achievement.id,
            payload={"awarded_by": None, "awarded_by_name": "Dream FC (old name)"},
        )

        self.assertIsNone(updated.awarded_by_id)
        self.assertEqual(
            updated.verification_status,
            Achievement.VerificationStatus.SELF_REPORTED
        )
        self.assertEqual(updated.awarded_by_name, "Dream FC (old name)")

    def test_unlinking_without_a_name_is_allowed(self):
        """Careers refuses this; an achievement with no issuer at all is normal."""
        achievement = self._create(awarded_by=self.club.id)

        updated = AchievementService.update_achievement(
            self.actor,
            achievement.id,
            payload={"awarded_by": None, "awarded_by_name": ""},
        )

        self.assertEqual(updated.awarded_by_name, "")
        self.assertEqual(
            updated.verification_status,
            Achievement.VerificationStatus.SELF_REPORTED
        )

    def test_name_edits_are_ignored_while_an_org_is_linked(self):
        """
        The column is derived there, and save() overwrites it — honouring the
        payload would count a doomed edit as material and reset a verification
        for nothing.
        """
        achievement = self._verified()

        updated = AchievementService.update_achievement(
            self.actor,
            achievement.id,
            payload={"awarded_by_name": "Something Else"},
        )

        self.assertEqual(updated.awarded_by_name, "Dream FC")
        self.assertEqual(
            updated.verification_status,
            Achievement.VerificationStatus.VERIFIED
        )

    def test_empty_payload_is_rejected(self):
        achievement = self._create()

        with self.assertRaises(ValidationError):
            AchievementService.update_achievement(
                self.actor, achievement.id, payload={}
            )

    def test_future_date_is_rejected_on_update(self):
        achievement = self._create()

        with self.assertRaises(ValidationError):
            AchievementService.update_achievement(
                self.actor,
                achievement.id,
                payload={"achieved_date": timezone.localdate() + timedelta(days=1)},
            )

    def test_cannot_update_someone_elses_achievement(self):
        achievement = self._create()
        stranger = Actor(actor_type="user", user=self.other)

        with self.assertRaises(PermissionDenied):
            AchievementService.update_achievement(
                stranger, achievement.id, payload={"title": "Mine now"}
            )

    def test_organization_actor_cannot_update(self):
        achievement = self._create()
        org_actor = self._org_actor(self.player, self.club)

        with self.assertRaises(PermissionDenied):
            AchievementService.update_achievement(
                org_actor, achievement.id, payload={"title": "Nope"}
            )

    def test_unknown_achievement_is_not_found(self):
        with self.assertRaises(NotFound):
            AchievementService.update_achievement(
                self.actor,
                "00000000-0000-0000-0000-000000000001",
                payload={"title": "X"},
            )

    def test_malformed_id_is_a_validation_error(self):
        with self.assertRaises(ValidationError):
            AchievementService.update_achievement(
                self.actor, "not-a-uuid", payload={"title": "X"}
            )


# =====================================================================
# DELETE
# =====================================================================

class DeleteAchievementTests(AchievementServiceTestCase):

    def test_delete_removes_the_row(self):
        """Hard delete — structured profile data, not user-authored content."""
        achievement = self._create()

        deleted_id = AchievementService.delete_achievement(
            self.actor, achievement.id
        )

        self.assertEqual(deleted_id, achievement.id)
        self.assertFalse(Achievement.objects.filter(id=achievement.id).exists())

    def test_delete_takes_dependent_notifications_with_it(self):
        """The FK cascade is what prevents notifications deep-linking to a dead id."""
        with self.captureOnCommitCallbacks(execute=True):
            achievement = self._create(awarded_by=self.club.id)

        self.assertEqual(
            Notification.objects.filter(achievement=achievement).count(), 1
        )

        AchievementService.delete_achievement(self.actor, achievement.id)

        self.assertEqual(Notification.objects.filter(achievement_id=achievement.id).count(), 0)

    def test_cannot_delete_someone_elses_achievement(self):
        achievement = self._create()
        stranger = Actor(actor_type="user", user=self.other)

        with self.assertRaises(PermissionDenied):
            AchievementService.delete_achievement(stranger, achievement.id)

        self.assertTrue(Achievement.objects.filter(id=achievement.id).exists())

    def test_missing_achievement_is_not_found(self):
        with self.assertRaises(NotFound):
            AchievementService.delete_achievement(
                self.actor, "00000000-0000-0000-0000-000000000001"
            )


# =====================================================================
# ORDERING & SELECTORS
# =====================================================================

class AchievementOrderingTests(AchievementServiceTestCase):

    def test_list_ordering(self):
        """
        is_pinned DESC, then achieved_date DESC, then created_at DESC — the
        model's own ordering, which for achievements IS the profile order.
        """
        self._create(title="old", achieved_date=date(2015, 1, 1))
        self._create(title="recent", achieved_date=date(2022, 1, 1))
        self._create(title="newest", achieved_date=date(2024, 1, 1))
        self._create(title="pinned", achieved_date=date(2010, 1, 1), is_pinned=True)

        titles = [a.title for a in list_for_user(self.player)]

        self.assertEqual(titles, ["pinned", "newest", "recent", "old"])

    def test_several_pins_sort_among_themselves_by_date(self):
        self._create(title="pin old", achieved_date=date(2018, 1, 1), is_pinned=True)
        self._create(title="pin new", achieved_date=date(2023, 1, 1), is_pinned=True)
        self._create(title="loose", achieved_date=date(2024, 1, 1))

        titles = [a.title for a in list_for_user(self.player)]

        self.assertEqual(titles, ["pin new", "pin old", "loose"])

    def test_ordering_is_per_user(self):
        self._create(title="mine")
        AchievementService.create_achievement(
            Actor(actor_type="user", user=self.other),
            payload=self._payload(title="theirs"),
        )

        titles = [a.title for a in list_for_user(self.player)]

        self.assertEqual(titles, ["mine"])

    def test_list_for_nobody_is_empty(self):
        self.assertEqual(list(list_for_user(None)), [])
        self.assertEqual(count_for_user(None), 0)

    def test_count_for_user(self):
        self._create(title="one")
        self._create(title="two")

        self.assertEqual(count_for_user(self.player), 2)
        self.assertEqual(count_for_user(self.other), 0)

    def test_get_by_id(self):
        achievement = self._create()

        self.assertEqual(get_by_id(achievement.id).id, achievement.id)
        self.assertIsNone(get_by_id("00000000-0000-0000-0000-000000000001"))
        self.assertIsNone(get_by_id(None))


# =====================================================================
# ORG VERIFICATION
# =====================================================================

class AchievementVerificationTestCase(AchievementServiceTestCase):
    """
    Adds the reviewer cast: an owner, a coach and a staff member at the crediting
    org, plus a second org that has nothing to do with the award.
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

        # A different org, with its own owner.
        self.rival = self._org("rivalfc", "Rival FC")
        self.rival_owner = self._user("rivalowner", User.Role.ORG_USER)
        self.rival_actor = self._org_actor(
            self.rival_owner, self.rival, OrganizationMember.Role.OWNER
        )

    def _pending_achievement(self, **overrides):
        """An award crediting self.club, sitting in the club's review queue."""
        with self.captureOnCommitCallbacks(execute=True):
            return self._create(awarded_by=self.club.id, **overrides)

    def _requests(self):
        return Notification.objects.filter(
            type=Notification.Type.ACHIEVEMENT_VERIFICATION_REQUEST
        )


class AchievementVerificationPermissionTests(AchievementVerificationTestCase):

    def test_wrong_org_is_rejected(self):
        """An org cannot act on an award that credits a different org."""
        achievement = self._pending_achievement()

        with self.assertRaises(PermissionDenied):
            AchievementVerificationService.verify(self.rival_actor, achievement.id)

        with self.assertRaises(PermissionDenied):
            AchievementVerificationService.reject(self.rival_actor, achievement.id)

        achievement.refresh_from_db()
        self.assertEqual(
            achievement.verification_status,
            Achievement.VerificationStatus.PENDING
        )

    def test_issuerless_award_is_nobodys_to_decide(self):
        """403, not 400 — it credits nobody, so it is not this org's row."""
        achievement = self._create()

        with self.assertRaises(PermissionDenied):
            AchievementVerificationService.verify(self.owner_actor, achievement.id)

    def test_coach_member_is_rejected(self):
        achievement = self._pending_achievement()

        with self.assertRaises(PermissionDenied):
            AchievementVerificationService.verify(self.coach_actor, achievement.id)

        achievement.refresh_from_db()
        self.assertEqual(
            achievement.verification_status,
            Achievement.VerificationStatus.PENDING
        )

    def test_staff_member_is_rejected(self):
        achievement = self._pending_achievement()

        with self.assertRaises(PermissionDenied):
            AchievementVerificationService.reject(self.staff_actor, achievement.id)

        achievement.refresh_from_db()
        self.assertEqual(
            achievement.verification_status,
            Achievement.VerificationStatus.PENDING
        )

    def test_user_actor_is_rejected(self):
        """Not even the award's own owner can verify their own claim."""
        achievement = self._pending_achievement()

        with self.assertRaises(PermissionDenied):
            AchievementVerificationService.verify(self.actor, achievement.id)

    def test_admin_member_is_allowed(self):
        achievement = self._pending_achievement()
        admin = self._user("clubadmin", User.Role.ORG_USER)
        admin_actor = self._org_actor(
            admin, self.club, OrganizationMember.Role.ADMIN
        )

        updated = AchievementVerificationService.verify(admin_actor, achievement.id)

        self.assertEqual(
            updated.verification_status,
            Achievement.VerificationStatus.VERIFIED
        )

    def test_review_queue_is_scoped_to_the_acting_org(self):
        achievement = self._pending_achievement()

        self.assertEqual(
            [a.id for a in pending_verification_requests_for(self.club)],
            [achievement.id],
        )
        self.assertEqual(
            list(pending_verification_requests_for(self.rival)),
            [],
        )

    def test_review_queue_excludes_decided_awards(self):
        achievement = self._pending_achievement()
        AchievementVerificationService.verify(self.owner_actor, achievement.id)

        self.assertEqual(list(pending_verification_requests_for(self.club)), [])

    def test_review_queue_is_oldest_first(self):
        """A work queue — the person waiting longest is dealt with first."""
        first = self._pending_achievement(title="First")
        second = self._pending_achievement(title="Second")
        third = self._pending_achievement(title="Third")

        self.assertEqual(
            [a.id for a in pending_verification_requests_for(self.club)],
            [first.id, second.id, third.id],
        )

    def test_owner_pins_do_not_reorder_the_work_queue(self):
        """Meta.ordering is overridden — a pin is a profile choice, not a priority."""
        first = self._pending_achievement(title="First")
        second = self._pending_achievement(title="Second")

        AchievementService.update_achievement(
            self.actor, second.id, payload={"is_pinned": True}
        )

        self.assertEqual(
            [a.id for a in pending_verification_requests_for(self.club)],
            [first.id, second.id],
        )

    def test_queue_helpers_gate_on_the_reviewer_role(self):
        self._pending_achievement()

        for actor in (self.coach_actor, self.staff_actor, self.actor, None):
            with self.subTest(actor=actor):
                with self.assertRaises(PermissionDenied):
                    AchievementVerificationService.list_pending_for_org(actor)
                with self.assertRaises(PermissionDenied):
                    AchievementVerificationService.list_decided_for_org(actor)

    def test_list_pending_for_org_matches_the_selector(self):
        achievement = self._pending_achievement()

        self.assertEqual(
            [a.id for a in AchievementVerificationService.list_pending_for_org(self.owner_actor)],
            [achievement.id],
        )


class AchievementVerificationTransitionTests(AchievementVerificationTestCase):

    def test_verify_transition(self):
        achievement = self._pending_achievement()

        with self.captureOnCommitCallbacks(execute=True):
            updated = AchievementVerificationService.verify(
                self.owner_actor, achievement.id
            )

        self.assertEqual(
            updated.verification_status,
            Achievement.VerificationStatus.VERIFIED
        )
        # The deciding PERSON, not the org — the trail survives them leaving.
        self.assertEqual(updated.verified_by_id, self.club_owner.id)
        self.assertIsNotNone(updated.verified_at)

        notification = Notification.objects.get(
            type=Notification.Type.ACHIEVEMENT_VERIFIED
        )
        self.assertEqual(notification.recipient_user_id, self.player.id)
        self.assertEqual(notification.actor_org_id, self.club.id)
        self.assertEqual(notification.achievement_id, achievement.id)

    def test_verified_decision_deep_links_to_the_owners_profile(self):
        """The org decided, but the award lives on the owner."""
        achievement = self._pending_achievement()

        with self.captureOnCommitCallbacks(execute=True):
            AchievementVerificationService.verify(self.owner_actor, achievement.id)

        payload = build_notification_payload(
            Notification.objects.get(type=Notification.Type.ACHIEVEMENT_VERIFIED)
        )

        self.assertEqual(payload["title"], "Dream FC verified your achievement ✅")
        # A fragment the profile page actually has a target for, not a `?tab=`
        # the page would ignore.
        self.assertEqual(payload["url"], "/profile/player#achievements")

    def test_reject_transition_carries_the_reason(self):
        achievement = self._pending_achievement()

        with self.captureOnCommitCallbacks(execute=True):
            updated = AchievementVerificationService.reject(
                self.owner_actor,
                achievement.id,
                reason="We have no record of issuing this."
            )

        self.assertEqual(
            updated.verification_status,
            Achievement.VerificationStatus.REJECTED
        )
        # Nobody verified anything, so the audit fields stay empty.
        self.assertIsNone(updated.verified_by_id)
        self.assertIsNone(updated.verified_at)

        notification = Notification.objects.get(
            type=Notification.Type.ACHIEVEMENT_REJECTED
        )
        self.assertEqual(notification.recipient_user_id, self.player.id)
        self.assertEqual(
            notification.data["reason"],
            "We have no record of issuing this."
        )
        self.assertEqual(
            build_notification_payload(notification)["body"],
            "We have no record of issuing this.",
        )

    def test_reject_reason_is_optional(self):
        achievement = self._pending_achievement()

        with self.captureOnCommitCallbacks(execute=True):
            AchievementVerificationService.reject(self.owner_actor, achievement.id)

        notification = Notification.objects.get(
            type=Notification.Type.ACHIEVEMENT_REJECTED
        )
        self.assertEqual(notification.data["reason"], "")
        self.assertIn(
            "is still shown as self-reported",
            build_notification_payload(notification)["body"],
        )

    def test_overlong_reason_is_rejected(self):
        achievement = self._pending_achievement()

        with self.assertRaises(ValidationError):
            AchievementVerificationService.reject(
                self.owner_actor, achievement.id, reason="x" * 201
            )

        achievement.refresh_from_db()
        self.assertEqual(
            achievement.verification_status,
            Achievement.VerificationStatus.PENDING
        )

    def test_cannot_verify_twice(self):
        """A no-op decision is refused — that is a double-submit, not intent."""
        achievement = self._pending_achievement()
        AchievementVerificationService.verify(self.owner_actor, achievement.id)

        with self.assertRaises(ValidationError) as ctx:
            AchievementVerificationService.verify(self.owner_actor, achievement.id)

        self.assertIn("already verified", str(ctx.exception.detail).lower())

    def test_cannot_reject_twice(self):
        achievement = self._pending_achievement()
        AchievementVerificationService.reject(self.owner_actor, achievement.id)

        with self.assertRaises(ValidationError) as ctx:
            AchievementVerificationService.reject(self.owner_actor, achievement.id)

        self.assertIn("already rejected", str(ctx.exception.detail).lower())

    def test_an_org_can_verify_what_it_previously_rejected(self):
        """Orgs learn things late — history is revisitable, not final."""
        achievement = self._pending_achievement()
        AchievementVerificationService.reject(
            self.owner_actor, achievement.id, reason="No record"
        )

        with self.captureOnCommitCallbacks(execute=True):
            updated = AchievementVerificationService.verify(
                self.owner_actor, achievement.id
            )

        self.assertEqual(
            updated.verification_status,
            Achievement.VerificationStatus.VERIFIED
        )
        self.assertEqual(updated.verified_by_id, self.club_owner.id)
        self.assertTrue(
            Notification.objects.filter(
                type=Notification.Type.ACHIEVEMENT_VERIFIED,
                recipient_user=self.player,
            ).exists()
        )

    def test_an_org_can_withdraw_a_verification(self):
        achievement = self._pending_achievement()
        AchievementVerificationService.verify(self.owner_actor, achievement.id)

        updated = AchievementVerificationService.reject(
            self.owner_actor, achievement.id, reason="Withdrawn after review"
        )

        self.assertEqual(
            updated.verification_status,
            Achievement.VerificationStatus.REJECTED
        )
        # The old confirmation's audit trail must not survive the withdrawal.
        self.assertIsNone(updated.verified_by_id)
        self.assertIsNone(updated.verified_at)

    def test_owner_can_still_edit_a_rejected_award(self):
        """A rejection is not a lock — the award stays the owner's to fix."""
        achievement = self._pending_achievement()
        AchievementVerificationService.reject(self.owner_actor, achievement.id)

        updated = AchievementService.update_achievement(
            self.actor, achievement.id, payload={"title": "Runner-up"}
        )

        self.assertEqual(updated.title, "Runner-up")

    def test_unknown_achievement_is_not_found(self):
        with self.assertRaises(NotFound):
            AchievementVerificationService.verify(
                self.owner_actor, "00000000-0000-0000-0000-000000000001"
            )

    def test_malformed_id_is_a_validation_error(self):
        with self.assertRaises(ValidationError):
            AchievementVerificationService.verify(self.owner_actor, "not-a-uuid")

    def test_the_full_loop(self):
        """
        create → pending → verify → material edit → pending again → reject.
        The whole point of the two services put together.
        """
        achievement = self._pending_achievement()
        self.assertEqual(
            achievement.verification_status,
            Achievement.VerificationStatus.PENDING
        )
        self.assertEqual(self._requests().count(), 1)

        with self.captureOnCommitCallbacks(execute=True):
            AchievementVerificationService.verify(self.owner_actor, achievement.id)
        achievement.refresh_from_db()
        self.assertEqual(
            achievement.verification_status,
            Achievement.VerificationStatus.VERIFIED
        )
        self.assertEqual(list(pending_verification_requests_for(self.club)), [])

        with self.captureOnCommitCallbacks(execute=True):
            AchievementService.update_achievement(
                self.actor, achievement.id, payload={"title": "Golden Glove"}
            )
        achievement.refresh_from_db()
        self.assertEqual(
            achievement.verification_status,
            Achievement.VerificationStatus.PENDING
        )
        self.assertIsNone(achievement.verified_by_id)
        self.assertEqual(self._requests().count(), 2)
        self.assertEqual(
            [a.id for a in pending_verification_requests_for(self.club)],
            [achievement.id],
        )

        with self.captureOnCommitCallbacks(execute=True):
            AchievementVerificationService.reject(
                self.owner_actor, achievement.id, reason="Cannot confirm the new title."
            )
        achievement.refresh_from_db()
        self.assertEqual(
            achievement.verification_status,
            Achievement.VerificationStatus.REJECTED
        )
        self.assertEqual(
            [a.id for a in decided_verification_requests_for(self.club)],
            [achievement.id],
        )


class AchievementVerificationNotificationTests(AchievementVerificationTestCase):

    def test_create_with_org_notifies_the_org(self):
        with self.captureOnCommitCallbacks(execute=True):
            achievement = self._create(awarded_by=self.club.id)

        notification = self._requests().get()
        self.assertEqual(notification.recipient_org_id, self.club.id)
        self.assertEqual(notification.actor_user_id, self.player.id)
        self.assertEqual(notification.achievement_id, achievement.id)
        self.assertEqual(
            notification.group_key,
            f"achievement_verification:org:{self.club.id}",
        )
        self.assertEqual(
            notification.data["achievement_title"], achievement.title
        )

    def test_request_push_copy(self):
        achievement = self._pending_achievement()

        payload = build_notification_payload(self._requests().get())

        self.assertEqual(payload["title"], "Player credited you with an achievement")
        self.assertIn(achievement.title, payload["body"])
        # The real route — one verifications page with a domain tab. There is no
        # /achievement-verifications route, and the service worker navigates to
        # this URL verbatim.
        self.assertEqual(
            payload["url"],
            f"/organization/admin/{self.club.id}/verifications?tab=achievements",
        )

    def test_issuerless_award_notifies_nobody(self):
        with self.captureOnCommitCallbacks(execute=True):
            self._create(awarded_by_name="Kerala FA")

        self.assertEqual(self._requests().count(), 0)

    def test_material_edit_of_a_verified_award_re_requests_exactly_once(self):
        """The headline case: a verified award edited back into the queue."""
        achievement = self._pending_achievement()
        self.assertEqual(self._requests().count(), 1)

        AchievementVerificationService.verify(self.owner_actor, achievement.id)

        with self.captureOnCommitCallbacks(execute=True):
            updated = AchievementService.update_achievement(
                self.actor, achievement.id, payload={"title": "Golden Glove"}
            )

        self.assertEqual(
            updated.verification_status,
            Achievement.VerificationStatus.PENDING
        )
        self.assertEqual(self._requests().count(), 2)
        self.assertEqual(
            [a.id for a in pending_verification_requests_for(self.club)],
            [achievement.id],
        )

    def test_date_edit_of_a_verified_award_re_requests(self):
        """Dates specifically — the field a reviewer is most likely to check."""
        achievement = self._pending_achievement()
        AchievementVerificationService.verify(self.owner_actor, achievement.id)
        before = self._requests().count()

        with self.captureOnCommitCallbacks(execute=True):
            updated = AchievementService.update_achievement(
                self.actor, achievement.id, payload={"achieved_date": date(2023, 6, 1)}
            )

        self.assertEqual(
            updated.verification_status,
            Achievement.VerificationStatus.PENDING
        )
        self.assertEqual(self._requests().count(), before + 1)

    def test_non_material_edit_of_a_verified_award_does_not_re_request(self):
        achievement = self._pending_achievement()
        AchievementVerificationService.verify(self.owner_actor, achievement.id)

        with self.captureOnCommitCallbacks(execute=True):
            AchievementService.update_achievement(
                self.actor,
                achievement.id,
                payload={"description": "19 goals.", "is_pinned": True},
            )

        self.assertEqual(self._requests().count(), 1)

    def test_edit_of_an_already_pending_award_does_not_re_request(self):
        """Still pending, still the same org — nothing new for the reviewer."""
        achievement = self._pending_achievement()

        with self.captureOnCommitCallbacks(execute=True):
            AchievementService.update_achievement(
                self.actor, achievement.id, payload={"title": "Golden Glove"}
            )

        self.assertEqual(self._requests().count(), 1)

    def test_re_linking_moves_the_request_to_the_new_org(self):
        """
        The old org is no longer being asked anything, so its request goes away
        — both from its queue (which reads through the FK) and from its
        notifications (which would otherwise linger as a dead invitation).
        """
        achievement = self._pending_achievement()
        self.assertEqual(self._requests().count(), 1)

        with self.captureOnCommitCallbacks(execute=True):
            AchievementService.update_achievement(
                self.actor, achievement.id, payload={"awarded_by": self.rival.id}
            )

        self.assertEqual(
            list(self._requests().values_list("recipient_org_id", flat=True)),
            [self.rival.id],
        )
        self.assertEqual(list(pending_verification_requests_for(self.club)), [])
        self.assertEqual(
            [a.id for a in pending_verification_requests_for(self.rival)],
            [achievement.id],
        )

    def test_unlinking_withdraws_the_request(self):
        achievement = self._pending_achievement()

        with self.captureOnCommitCallbacks(execute=True):
            AchievementService.update_achievement(
                self.actor, achievement.id, payload={"awarded_by": None}
            )

        self.assertEqual(self._requests().count(), 0)
        self.assertEqual(list(pending_verification_requests_for(self.club)), [])

    def test_re_linking_keeps_the_old_orgs_decisions(self):
        """Only the REQUEST is withdrawn — a decision already made is their record."""
        achievement = self._pending_achievement()
        with self.captureOnCommitCallbacks(execute=True):
            AchievementVerificationService.reject(self.owner_actor, achievement.id)

        with self.captureOnCommitCallbacks(execute=True):
            AchievementService.update_achievement(
                self.actor, achievement.id, payload={"awarded_by": self.rival.id}
            )

        self.assertTrue(
            Notification.objects.filter(
                type=Notification.Type.ACHIEVEMENT_REJECTED,
                recipient_user=self.player,
            ).exists()
        )

    def test_a_decided_award_leaves_requests_and_enters_history(self):
        """The two tabs partition the org's rows — never both, never neither."""
        achievement = self._pending_achievement()
        AchievementVerificationService.verify(self.owner_actor, achievement.id)

        self.assertEqual(list(pending_verification_requests_for(self.club)), [])
        self.assertEqual(
            [a.id for a in decided_verification_requests_for(self.club)],
            [achievement.id],
        )

    def test_editing_a_verified_award_moves_it_back_to_requests(self):
        achievement = self._pending_achievement()
        AchievementVerificationService.verify(self.owner_actor, achievement.id)

        with self.captureOnCommitCallbacks(execute=True):
            AchievementService.update_achievement(
                self.actor, achievement.id, payload={"title": "Golden Glove"}
            )

        self.assertEqual(
            [a.id for a in pending_verification_requests_for(self.club)],
            [achievement.id],
        )
        self.assertEqual(list(decided_verification_requests_for(self.club)), [])

    def test_deleting_an_award_takes_its_notifications_with_it(self):
        """Achievements hard-delete, so the FK cascade prevents dead links."""
        achievement = self._pending_achievement()
        with self.captureOnCommitCallbacks(execute=True):
            AchievementVerificationService.verify(self.owner_actor, achievement.id)

        self.assertEqual(
            Notification.objects.filter(achievement=achievement).count(), 2
        )

        AchievementService.delete_achievement(self.actor, achievement.id)

        self.assertEqual(
            Notification.objects.filter(achievement_id=achievement.id).count(), 0
        )


# =====================================================================
# HTTP WIRING
# =====================================================================

class AchievementAPITests(AchievementServiceTestCase, APITestCase):
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

    def test_create_read_list_patch_delete_round_trip(self):
        entry = self._career_entry()
        headers = self._auth(self.player)

        created = self.client.post(
            "/achievements/create",
            {
                "sport": str(self.football.id),
                "awarded_by": str(self.club.id),
                "career_entry": str(entry.id),
                "title": "Golden Boot",
                "achievement_type": "individual_award",
                "event_name": "Kerala Premier League 2024",
                "level": "state",
                "achieved_date": "2024-05-01",
                "reference_link": "https://example.com/report",
            },
            format="json",
            **headers,
        )
        self.assertEqual(created.status_code, 201, created.data)

        body = created.data["data"]
        achievement_id = body["id"]

        # The full serialized shape, nested objects included.
        self.assertEqual(body["awarded_by"]["username"], "dreamfc")
        self.assertEqual(body["awarded_by"]["name"], "Dream FC")
        self.assertIn("logo", body["awarded_by"])
        self.assertEqual(body["awarded_by_name"], "Dream FC")
        self.assertEqual(body["sport"]["name"], "Football")
        self.assertEqual(body["career_entry"]["title"], "Player")
        self.assertEqual(body["career_entry"]["organization_name"], "Dream FC")
        self.assertEqual(body["event_name"], "Kerala Premier League 2024")
        self.assertEqual(body["level"], "state")
        self.assertEqual(body["achieved_date"], "2024-05-01")
        self.assertEqual(body["verification_status"], "pending")
        self.assertFalse(body["is_pinned"])

        # Detail read — the same shape as a list row.
        detail = self.client.get(f"/achievements/{achievement_id}", **headers)
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.data["data"], body)

        # Public read — an org actor sees the same shelf.
        listed = self.client.get(
            f"/achievements/users/{self.player.id}",
            **self._auth(self.other, self._org_actor(self.other, self.club).organization),
        )
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.data["data"]["count"], 1)
        self.assertFalse(listed.data["data"]["is_owner"])

        # PATCH doubles as pin/unpin.
        patched = self.client.patch(
            f"/achievements/{achievement_id}",
            {"is_pinned": True, "description": "19 goals."},
            format="json",
            **self._auth(self.player),
        )
        self.assertEqual(patched.status_code, 200, patched.data)
        self.assertTrue(patched.data["data"]["is_pinned"])
        self.assertEqual(patched.data["data"]["description"], "19 goals.")

        removed = self.client.delete(
            f"/achievements/{achievement_id}", **self._auth(self.player)
        )
        self.assertEqual(removed.status_code, 200)
        self.assertFalse(Achievement.objects.filter(id=achievement_id).exists())

    def test_list_marks_the_owner(self):
        self._create()

        listed = self.client.get(
            f"/achievements/users/{self.player.id}", **self._auth(self.player)
        )

        self.assertTrue(listed.data["data"]["is_owner"])

    def test_list_for_an_unknown_user_is_404(self):
        response = self.client.get(
            "/achievements/users/00000000-0000-0000-0000-000000000001",
            **self._auth(self.player),
        )

        self.assertEqual(response.status_code, 404)

    def test_list_is_pinned_first(self):
        self._auth(self.player)
        self._create(title="old", achieved_date=date(2020, 1, 1))
        self._create(title="new", achieved_date=date(2024, 1, 1))
        self._create(title="pinned", achieved_date=date(2019, 1, 1), is_pinned=True)

        response = self.client.get(f"/achievements/users/{self.player.id}")

        self.assertEqual(
            [row["title"] for row in response.data["data"]["results"]],
            ["pinned", "new", "old"],
        )

    def test_org_actor_write_is_403_before_body_validation(self):
        # A *verified* member acting as the org — so the 403 comes from the
        # achievement rule, not from resolve_actor rejecting the membership.
        self._org_actor(self.player, self.club)

        response = self.client.post(
            "/achievements/create",
            {"garbage": True},
            format="json",
            **self._auth(self.player, self.club),
        )

        self.assertEqual(response.status_code, 403)

    def test_org_actor_cannot_patch_or_delete(self):
        achievement = self._create()
        self._org_actor(self.player, self.club)
        headers = self._auth(self.player, self.club)

        self.assertEqual(
            self.client.patch(
                f"/achievements/{achievement.id}",
                {"title": "Nope"},
                format="json",
                **headers,
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.delete(f"/achievements/{achievement.id}", **headers).status_code,
            403,
        )

    def test_patching_someone_elses_achievement_is_403(self):
        achievement = self._create()

        response = self.client.patch(
            f"/achievements/{achievement.id}",
            {"title": "Mine now"},
            format="json",
            **self._auth(self.other),
        )

        self.assertEqual(response.status_code, 403)

    def test_bad_body_is_400_with_field_errors(self):
        response = self.client.post(
            "/achievements/create",
            {"sport": str(self.football.id)},
            format="json",
            **self._auth(self.player),
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("title", response.data["data"]["errors"])
        self.assertIn("achieved_date", response.data["data"]["errors"])

    def test_empty_patch_is_400(self):
        achievement = self._create()

        response = self.client.patch(
            f"/achievements/{achievement.id}",
            {},
            format="json",
            **self._auth(self.player),
        )

        self.assertEqual(response.status_code, 400)

    def test_unknown_achievement_is_404(self):
        headers = self._auth(self.player)

        self.assertEqual(
            self.client.get(
                "/achievements/00000000-0000-0000-0000-000000000001", **headers
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.delete(
                "/achievements/00000000-0000-0000-0000-000000000001", **headers
            ).status_code,
            404,
        )

    def test_routes_carry_no_trailing_slash(self):
        """
        A trailing slash 308s at Vercel then APPEND_SLASHes back to a path-only
        Location that resolves against the frontend origin — a production-only
        404. The slash-less form is the contract.
        """
        achievement = self._create()
        headers = self._auth(self.player)

        for path in (
            f"/achievements/users/{self.player.id}",
            f"/achievements/{achievement.id}",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path, **headers).status_code, 200)


class AchievementVerificationAPITests(AchievementVerificationTestCase, APITestCase):
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
        achievement = self._pending_achievement(
            event_name="Kerala Premier League 2024",
            level=Achievement.Level.STATE,
            reference_link="https://example.com/report",
        )

        queue = self.client.get(
            "/achievements/verification-requests",
            **self._auth(self.club_owner, self.club),
        )
        self.assertEqual(queue.status_code, 200)
        self.assertEqual(queue.data["data"]["count"], 1)

        row = queue.data["data"]["results"][0]
        self.assertEqual(row["user"]["username"], "player")
        self.assertEqual(row["user"]["role"], User.Role.PLAYER)
        self.assertEqual(row["sport"]["name"], "Football")
        self.assertEqual(row["title"], "Golden Boot")
        self.assertEqual(row["event_name"], "Kerala Premier League 2024")
        self.assertEqual(row["level"], "state")
        self.assertEqual(row["reference_link"], "https://example.com/report")
        # The reviewer's card deliberately drops these.
        self.assertNotIn("is_pinned", row)
        self.assertNotIn("awarded_by", row)

        with self.captureOnCommitCallbacks(execute=True):
            verified = self.client.post(
                f"/achievements/{achievement.id}/verify",
                {},
                format="json",
                **self._auth(self.club_owner, self.club),
            )

        self.assertEqual(verified.status_code, 200)
        self.assertEqual(verified.data["data"]["verification_status"], "verified")
        self.assertTrue(
            Notification.objects.filter(
                type=Notification.Type.ACHIEVEMENT_VERIFIED,
                recipient_user=self.player,
            ).exists()
        )

    def test_reject_with_reason(self):
        achievement = self._pending_achievement()

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                f"/achievements/{achievement.id}/reject",
                {"reason": "Not one of ours."},
                format="json",
                **self._auth(self.club_owner, self.club),
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["data"]["verification_status"], "rejected")

        notification = Notification.objects.get(
            type=Notification.Type.ACHIEVEMENT_REJECTED
        )
        self.assertEqual(notification.data["reason"], "Not one of ours.")

    def test_coach_gets_403_on_every_endpoint(self):
        achievement = self._pending_achievement()
        headers = self._auth(self.club_coach, self.club)

        self.assertEqual(
            self.client.get(
                "/achievements/verification-requests", **headers
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                f"/achievements/{achievement.id}/verify", {}, format="json", **headers
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                f"/achievements/{achievement.id}/reject", {}, format="json", **headers
            ).status_code,
            403,
        )

    def test_staff_gets_403_on_every_endpoint(self):
        achievement = self._pending_achievement()
        headers = self._auth(self.club_staff, self.club)

        self.assertEqual(
            self.client.get(
                "/achievements/verification-requests", **headers
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                f"/achievements/{achievement.id}/verify", {}, format="json", **headers
            ).status_code,
            403,
        )

    def test_wrong_org_gets_403(self):
        achievement = self._pending_achievement()

        response = self.client.post(
            f"/achievements/{achievement.id}/verify",
            {},
            format="json",
            **self._auth(self.rival_owner, self.rival),
        )

        self.assertEqual(response.status_code, 403)

    def test_verifying_a_decided_award_is_400(self):
        achievement = self._pending_achievement()
        AchievementVerificationService.verify(self.owner_actor, achievement.id)

        response = self.client.post(
            f"/achievements/{achievement.id}/verify",
            {},
            format="json",
            **self._auth(self.club_owner, self.club),
        )

        self.assertEqual(response.status_code, 400)

    def test_unknown_achievement_is_404(self):
        response = self.client.post(
            "/achievements/00000000-0000-0000-0000-000000000001/verify",
            {},
            format="json",
            **self._auth(self.club_owner, self.club),
        )

        self.assertEqual(response.status_code, 404)

    def test_queue_tabs_and_pagination(self):
        """Requests vs History, paginated, with an honest total."""
        pending = [self._pending_achievement(title=f"a{i}") for i in range(3)]
        AchievementVerificationService.verify(self.owner_actor, pending[0].id)

        headers = self._auth(self.club_owner, self.club)

        requests = self.client.get(
            "/achievements/verification-requests?status=pending&limit=1", **headers
        )
        self.assertEqual(requests.status_code, 200)
        self.assertEqual(requests.data["data"]["count"], 2)
        self.assertEqual(len(requests.data["data"]["results"]), 1)
        self.assertTrue(requests.data["data"]["has_more"])

        page2 = self.client.get(
            "/achievements/verification-requests?status=pending&limit=1&offset=1",
            **headers,
        )
        self.assertEqual(len(page2.data["data"]["results"]), 1)
        self.assertFalse(page2.data["data"]["has_more"])
        self.assertNotEqual(
            page2.data["data"]["results"][0]["id"],
            requests.data["data"]["results"][0]["id"],
        )

        history = self.client.get(
            "/achievements/verification-requests?status=decided", **headers
        )
        self.assertEqual(history.data["data"]["count"], 1)
        self.assertEqual(
            history.data["data"]["results"][0]["id"], str(pending[0].id)
        )

    def test_queue_rejects_an_unknown_status(self):
        response = self.client.get(
            "/achievements/verification-requests?status=nonsense",
            **self._auth(self.club_owner, self.club),
        )

        self.assertEqual(response.status_code, 400)

    def test_decision_can_be_changed_from_history(self):
        achievement = self._pending_achievement()
        headers = self._auth(self.club_owner, self.club)

        self.client.post(
            f"/achievements/{achievement.id}/reject", {}, format="json", **headers
        )

        with self.captureOnCommitCallbacks(execute=True):
            flipped = self.client.post(
                f"/achievements/{achievement.id}/verify", {}, format="json", **headers
            )

        self.assertEqual(flipped.status_code, 200)
        self.assertEqual(flipped.data["data"]["verification_status"], "verified")

    def test_user_actor_gets_403_on_the_queue(self):
        response = self.client.get(
            "/achievements/verification-requests", **self._auth(self.player)
        )

        self.assertEqual(response.status_code, 403)

    def test_verification_requests_route_beats_the_uuid_catch_all(self):
        """Route order: the literal path must not be swallowed by <uuid>."""
        response = self.client.get(
            "/achievements/verification-requests",
            **self._auth(self.club_owner, self.club),
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("results", response.data["data"])


# =====================================================================
# PERMISSION MATRIX
# =====================================================================

class AchievementPermissionMatrixTests(AchievementVerificationTestCase):
    """One test per cell of the matrix the API enforces."""

    def test_signed_out_actor_is_rejected_on_every_write(self):
        """`actor=None` is what resolve_actor yields for an anonymous request."""
        achievement = self._create()

        with self.assertRaises(PermissionDenied):
            AchievementService.create_achievement(None, payload=self._payload())

        with self.assertRaises(PermissionDenied):
            AchievementService.update_achievement(
                None, achievement.id, payload={"title": "X"}
            )

        with self.assertRaises(PermissionDenied):
            AchievementService.delete_achievement(None, achievement.id)

        with self.assertRaises(PermissionDenied):
            AchievementVerificationService.verify(None, achievement.id)

        with self.assertRaises(PermissionDenied):
            AchievementVerificationService.reject(None, achievement.id)

    def test_org_actor_cannot_delete(self):
        achievement = self._create()

        with self.assertRaises(PermissionDenied):
            AchievementService.delete_achievement(self.owner_actor, achievement.id)

        self.assertTrue(Achievement.objects.filter(id=achievement.id).exists())

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
        achievement = self._pending_achievement()

        with self.assertRaises(PermissionDenied):
            AchievementVerificationService.verify(rogue, achievement.id)

    def test_coach_and_staff_are_refused_both_decisions(self):
        for actor in (self.coach_actor, self.staff_actor):
            achievement = self._pending_achievement()

            with self.assertRaises(PermissionDenied):
                AchievementVerificationService.verify(actor, achievement.id)
            with self.assertRaises(PermissionDenied):
                AchievementVerificationService.reject(actor, achievement.id)

            achievement.refresh_from_db()
            self.assertEqual(
                achievement.verification_status,
                Achievement.VerificationStatus.PENDING
            )

    def test_coach_and_staff_cannot_read_the_queue(self):
        """The role gate covers the queue, not only the decisions."""
        for actor in (self.coach_actor, self.staff_actor):
            with self.assertRaises(PermissionDenied):
                AchievementVerificationService.require_reviewer(actor)

    def test_coach_and_scout_can_manage_their_own_achievements(self):
        """
        Achievements are NOT players-only. A coach's badge and a scout's
        certification are exactly what the `certification` type is for.
        """
        for username, role in (("acoach", User.Role.COACH), ("ascout", User.Role.SCOUT)):
            with self.subTest(role=role):
                user = self._user(username, role)
                actor = Actor(actor_type="user", user=user)

                achievement = AchievementService.create_achievement(
                    actor,
                    payload=self._payload(
                        title="UEFA B Licence",
                        achievement_type=Achievement.AchievementType.CERTIFICATION,
                    ),
                )
                self.assertEqual(achievement.user_id, user.id)

                updated = AchievementService.update_achievement(
                    actor, achievement.id, payload={"title": "UEFA A Licence"}
                )
                self.assertEqual(updated.title, "UEFA A Licence")

                AchievementService.delete_achievement(actor, achievement.id)
                self.assertFalse(
                    Achievement.objects.filter(id=achievement.id).exists()
                )


# =====================================================================
# QUERY BUDGETS (N+1 guards)
# =====================================================================

class AchievementQueryBudgetTests(AchievementVerificationTestCase):
    """
    A dropped select_related shows up as a query count that GROWS with the row
    count, so that — not an absolute number — is what these assert. They render
    through the real serializers, because the joins only pay off if they cover
    the fields the response actually reads.
    """

    def _achievements(self, count):
        entry = self._career_entry()
        for index in range(count):
            self._create(
                title=f"award {index}",
                awarded_by=self.club.id,
                career_entry=entry.id,
            )

    def _count_queries(self, render):
        with CaptureQueriesContext(connection) as ctx:
            render()
        return len(ctx.captured_queries)

    def _assert_constant(self, render):
        """Same query count for one row as for five."""
        self._achievements(1)
        one = self._count_queries(render)

        Achievement.objects.all().delete()

        self._achievements(5)
        five = self._count_queries(render)

        self.assertEqual(
            one,
            five,
            f"query count grew with rows ({one} → {five}) — a join is missing",
        )
        self.assertLessEqual(five, 2)

    def test_achievement_list_is_constant_query_count(self):
        self._assert_constant(
            lambda: AchievementSerializer(
                list(list_for_user(self.player)), many=True
            ).data
        )

    def test_verification_queue_is_constant_query_count(self):
        self._assert_constant(
            lambda: AchievementVerificationRequestSerializer(
                list(pending_verification_requests_for(self.club)), many=True
            ).data
        )

    def test_detail_read_needs_no_extra_queries(self):
        achievement = self._create(
            awarded_by=self.club.id, career_entry=self._career_entry().id
        )

        loaded = get_by_id(achievement.id)

        with self.assertNumQueries(0):
            AchievementSerializer(loaded).data

    def test_decision_loads_everything_the_response_needs(self):
        """
        verify() must return a row the serializer can render without going back
        to the database for the claimant's profile or the sport.
        """
        achievement = self._create(awarded_by=self.club.id)

        verified = AchievementVerificationService.verify(
            self.owner_actor, achievement.id
        )

        with self.assertNumQueries(0):
            AchievementVerificationRequestSerializer(verified).data


# =====================================================================
# NOTIFICATION GROUPING
# =====================================================================

class AchievementNotificationGroupingTests(AchievementVerificationTestCase):
    """
    The achievement types have two grouping shapes, each of which can fail in a
    different way:

      achievement_verification_request → grouped per ORG (many people → one row)
      achievement_verified / rejected  → never grouped (each decision is its own)
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

    def test_two_requests_from_one_person_list_them_once(self):
        """
        The same bug careers guards: actors collected per ROW would render as
        "Player, Player credited you with an achievement".
        """
        with self.captureOnCommitCallbacks(execute=True):
            self._create(title="Golden Boot", awarded_by=self.club.id)
            self._create(title="Player of the Season", awarded_by=self.club.id)

        groups = self._grouped(recipient_org=self.club)

        self.assertEqual(len(groups), 1)
        group = groups[0]
        self.assertEqual(len(group["actors"]), 1)
        self.assertEqual(group["others_count"], 0)
        self.assertEqual(group["text"], "Player credited you with an achievement")

    def test_requests_from_two_people_group_with_both_actors(self):
        with self.captureOnCommitCallbacks(execute=True):
            self._create(awarded_by=self.club.id)
            AchievementService.create_achievement(
                Actor(actor_type="user", user=self.other),
                payload=self._payload(awarded_by=self.club.id),
            )

        groups = self._grouped(recipient_org=self.club)

        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]["actors"]), 2)
        self.assertIn("credited you with an achievement", groups[0]["text"])

    def test_a_third_person_becomes_an_others_count(self):
        third = self._user("third")

        with self.captureOnCommitCallbacks(execute=True):
            self._create(awarded_by=self.club.id)
            for user in (self.other, third):
                AchievementService.create_achievement(
                    Actor(actor_type="user", user=user),
                    payload=self._payload(awarded_by=self.club.id),
                )

        group = self._grouped(recipient_org=self.club)[0]

        self.assertEqual(len(group["actors"]), 2)
        self.assertEqual(group["others_count"], 1)
        self.assertIn("and 1 others", group["text"])

    def test_decisions_never_collapse_into_one_row(self):
        """
        An org that verifies one award and rejects another must leave the owner
        two separate rows — they say opposite things.
        """
        first = self._pending_achievement(title="Golden Boot")
        second = self._pending_achievement(title="Player of the Season")

        with self.captureOnCommitCallbacks(execute=True):
            AchievementVerificationService.verify(self.owner_actor, first.id)
            AchievementVerificationService.reject(
                self.owner_actor, second.id, reason="Not ours"
            )

        groups = self._grouped(recipient_user=self.player)
        types = {group["type"] for group in groups}

        self.assertEqual(len(groups), 2)
        self.assertEqual(
            types,
            {
                Notification.Type.ACHIEVEMENT_VERIFIED,
                Notification.Type.ACHIEVEMENT_REJECTED,
            },
        )
        self.assertEqual(
            sorted(g["text"] for g in groups),
            [
                "Dream FC could not verify your achievement",
                "Dream FC verified your achievement ✅",
            ],
        )

    def test_two_verifications_stay_two_rows(self):
        """Same type, same actor — still two decisions, so still two rows."""
        first = self._pending_achievement(title="Golden Boot")
        second = self._pending_achievement(title="Player of the Season")

        with self.captureOnCommitCallbacks(execute=True):
            AchievementVerificationService.verify(self.owner_actor, first.id)
            AchievementVerificationService.verify(self.owner_actor, second.id)

        self.assertEqual(len(self._grouped(recipient_user=self.player)), 2)

    def test_grouped_response_exposes_the_data_payload(self):
        """
        The achievement id lives in `data`, and it is what the client needs to
        deep-link the row.
        """
        achievement = self._pending_achievement()

        group = self._grouped(recipient_org=self.club)[0]

        self.assertEqual(group["data"]["achievement_id"], str(achievement.id))
        self.assertEqual(group["data"]["achievement_title"], achievement.title)

    def test_career_and_achievement_requests_do_not_share_a_group(self):
        """
        Both are 'somebody wants you to confirm something' and both group per
        org — but they are different queues and must stay different rows.
        """
        with self.captureOnCommitCallbacks(execute=True):
            self._create(awarded_by=self.club.id)
            CareerEntry.objects.create(
                user=self.player,
                organization=self.club,
                organization_name="Dream FC",
                sport=self.football,
                title="Player",
                start_date=date(2020, 1, 1),
            )
            Notification.objects.create(
                type=Notification.Type.CAREER_VERIFICATION_REQUEST,
                group_key=f"career_verification:org:{self.club.id}",
                actor_user=self.player,
                recipient_org=self.club,
            )

        groups = self._grouped(recipient_org=self.club)

        self.assertEqual(len(groups), 2)
        self.assertEqual(
            {g["type"] for g in groups},
            {
                Notification.Type.ACHIEVEMENT_VERIFICATION_REQUEST,
                Notification.Type.CAREER_VERIFICATION_REQUEST,
            },
        )
