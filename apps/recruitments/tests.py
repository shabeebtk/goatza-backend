import uuid
from decimal import Decimal
from datetime import timedelta
from unittest.mock import patch

from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.test import override_settings
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from core.actor import Actor
from apps.accounts.models import User, UserProfile
from apps.organization.models import Organization, OrganizationMember
from apps.connections.models import Follow
from apps.usernames.services.username_service import UsernameService
from apps.sports.models import Sport, SportPosition, UserSport, UserSportPosition
from apps.recruitments.models import (
    Recruitment,
    RecruitmentMedia,
    RecruitmentPosition,
    RecruitmentApplication,
    RecruitmentApplicationAnswer,
    RecruitmentApplicationStatusHistory,
    RecruitmentQuestion,
    RecruitmentAgeCategory,
    RecruitmentEligibilityCriteria,
    SavedRecruitment,
)
from apps.recruitments.selectors.recruitment_selectors import RecruitmentSelector
from apps.recruitments.selectors.player_context_selectors import (
    PlayerContext, PlayerContextSelector,
)
from apps.recruitments.services import eligibility_service
from apps.recruitments.services.discover_service import SECTION_ORDER
from apps.recruitments.services.match_score_service import (
    MATCH_WEIGHTS, MatchScoreService,
)
from apps.notifications.models import Notification
from apps.legal.testing import accept_current_terms

SIGNATURE_URL = "/user/get/upload/signature"
CREATE_URL = "/recruitments/create"

# Every open_trial now needs at least one trial date, so the create/update
# payload helpers below carry one. Far enough out that a "deadline in the
# past" test still fails on the deadline and not on the date.
FUTURE_SESSIONS = [{"date": "2030-06-15"}]

# Deterministic media host so URL validation is env-independent.
CLOUD = "democloud"


class RecruitmentMediaPipelineTests(APITestCase):

    def setUp(self):
        self.user = User.objects.create_user(
            email="owner@example.com",
            password="pass1234",
            username="owner",
        )
        accept_current_terms(self.user)
        self.other_user = User.objects.create_user(
            email="stranger@example.com",
            password="pass1234",
            username="stranger",
        )
        accept_current_terms(self.other_user)

        self.org = Organization.objects.create(
            name="Dream FC",
            username="dreamfc",
            type=Organization.Type.CLUB,
        )
        OrganizationMember.objects.create(
            organization=self.org,
            user=self.user,
            role=OrganizationMember.Role.OWNER,
        )

        self.other_org = Organization.objects.create(
            name="Rival FC",
            username="rivalfc",
            type=Organization.Type.CLUB,
        )

        self.sport = Sport.objects.create(name="Football", icon_name="mdi:soccer")
        self.position = SportPosition.objects.create(sport=self.sport, name="Striker")

        self.client.force_authenticate(user=self.user)

    # ── helpers ──────────────────────────────────────────────────

    def _org_headers(self):
        return {
            "HTTP_X_ACTOR_TYPE": "organization",
            "HTTP_X_ACTOR_ID": str(self.org.id),
        }

    def _public_id(self, org=None):
        org = org or self.org
        return (
            f"organizations/{org.id}/recruitments/"
            f"{uuid.uuid4()}/{uuid.uuid4()}"
        )

    def _cloud_url(self, public_id, ext="jpg"):
        return (
            f"https://media.goatza.test/"
            f"v1/{public_id}.{ext}"
        )

    def _valid_media(self, media_type="image", ext="jpg"):
        public_id = self._public_id()
        return {
            "file_url": self._cloud_url(public_id, ext),
            "public_id": public_id,
            "media_type": media_type,
            "order": 0,
        }

    def _create_payload(self, media):
        return {
            "title": "U17 Open Trials",
            "short_description": "Trials for U17 players in the district.",
            "recruitment_type": "open_trial",
            "sport_id": str(self.sport.id),
            "positions": [
                {"position_id": str(self.position.id), "is_primary": True}
            ],
            "sessions": FUTURE_SESSIONS,
            "media": media,
        }

    def _create(self, media):
        return self.client.post(
            CREATE_URL,
            self._create_payload(media),
            format="json",
            **self._org_headers(),
        )

    # ── long URL regression ──────────────────────────────────────

    def test_create_accepts_long_url(self):
        # Real stored URLs (deep recruitment folder
        # path) exceed the old 200-char URLField default. Regression for
        # "value too long for type character varying(200)".
        public_id = self._public_id()
        long_url = (
            f"https://media.goatza.test/"
            f"v1783015360/{public_id}.jpg"
        )
        self.assertGreater(len(long_url), 200)

        resp = self._create([
            {
                "file_url": long_url,
                "public_id": public_id,
                "media_type": "image",
                "order": 0,
            }
        ])

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        recruitment_id = resp.data["data"]["recruitment_id"]
        media = RecruitmentMedia.objects.get(recruitment_id=recruitment_id)
        self.assertEqual(media.file_url, long_url)

    # ── signature endpoint ───────────────────────────────────────

    def test_signature_recruitments_org_member_ok(self):
        resp = self.client.get(
            SIGNATURE_URL,
            {"type": "recruitments", "count": 2, "org_id": str(self.org.id)},
            **self._org_headers(),
        )

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.data["data"]
        self.assertEqual(data["provider"], "r2")
        self.assertIn("temp_post_id", data)
        self.assertEqual(len(data["uploads"]), 2)
        self.assertTrue(
            data["uploads"][0]["folder"].startswith(
                f"organizations/{self.org.id}/recruitments/"
            )
        )

    def test_signature_recruitments_plain_user_forbidden(self):
        # No org headers / org_id → resolves to a plain user actor.
        resp = self.client.get(
            SIGNATURE_URL,
            {"type": "recruitments", "count": 1},
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    # ── create: media verification ───────────────────────────────

    def test_create_accepts_valid_media(self):
        resp = self._create([self._valid_media()])

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        recruitment_id = resp.data["data"]["recruitment_id"]
        self.assertEqual(
            RecruitmentMedia.objects.filter(
                recruitment_id=recruitment_id
            ).count(),
            1,
        )

    def test_create_rejects_foreign_org_public_id(self):
        foreign_public_id = self._public_id(org=self.other_org)
        media = {
            "file_url": self._cloud_url(foreign_public_id),
            "public_id": foreign_public_id,
            "media_type": "image",
            "order": 0,
        }

        resp = self._create([media])

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("does not belong to this organization", resp.data["error"])
        self.assertFalse(Recruitment.objects.exists())

    def test_create_rejects_foreign_url(self):
        public_id = self._public_id()
        media = {
            "file_url": f"https://evil.example.com/upload/v1/{public_id}.jpg",
            "public_id": public_id,
            "media_type": "image",
            "order": 0,
        }

        resp = self._create([media])

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("allowed media source", resp.data["error"])

    def test_create_rejects_url_public_id_mismatch(self):
        public_id = self._public_id()
        other_public_id = self._public_id()  # different path → mismatch
        media = {
            "file_url": self._cloud_url(other_public_id),
            "public_id": public_id,
            "media_type": "image",
            "order": 0,
        }

        resp = self._create([media])

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("does not match", resp.data["error"])

    def test_create_rejects_disallowed_extension(self):
        # A .gif is not in the image whitelist.
        media = self._valid_media(media_type="image", ext="gif")

        resp = self._create([media])

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("unsupported", resp.data["error"])

    # ── update: orphaned object cleanup ─────────────────────────

    def test_update_deletes_orphaned_assets(self):
        recruitment = Recruitment.objects.create(
            organization=self.org,
            sport=self.sport,
            status=Recruitment.Status.ACTIVE,
            title="Original",
            recruitment_type="open_trial",
        )

        keep_public_id = self._public_id()
        orphan_public_id = self._public_id()
        RecruitmentMedia.objects.create(
            recruitment=recruitment,
            file_url=self._cloud_url(keep_public_id),
            public_id=keep_public_id,
            media_type="image",
            order=0,
        )
        RecruitmentMedia.objects.create(
            recruitment=recruitment,
            file_url=self._cloud_url(orphan_public_id),
            public_id=orphan_public_id,
            media_type="image",
            order=1,
        )

        # Update payload keeps only the first asset → the second is orphaned.
        payload = self._create_payload(
            [
                {
                    "file_url": self._cloud_url(keep_public_id),
                    "public_id": keep_public_id,
                    "media_type": "image",
                    "order": 0,
                }
            ]
        )
        update_url = f"/recruitments/{recruitment.id}/update"

        with patch(
            "apps.recruitments.services.recruitment_service.get_storage_service"
        ) as mock_get_storage:
            mock_storage = mock_get_storage.return_value
            with self.captureOnCommitCallbacks(execute=True):
                resp = self.client.patch(
                    update_url,
                    payload,
                    format="json",
                    **self._org_headers(),
                )

            self.assertEqual(resp.status_code, status.HTTP_200_OK)
            mock_storage.delete_file.assert_called_once_with(orphan_public_id)


class RecruitmentValidationTests(APITestCase):
    """Draft flow, deadline rules, applicant-safe edits, structured errors."""

    def setUp(self):
        cache.clear()  # username→profile lookups are cached
        self.user = User.objects.create_user(
            email="owner2@example.com",
            password="pass1234",
            username="owner2",
        )
        accept_current_terms(self.user)
        self.org = Organization.objects.create(
            name="Kite FC",
            username="kitefc",
            type=Organization.Type.CLUB,
        )
        # These tests list recruitments BY the org's handle, and a handle only
        # resolves once UsernameRegistry holds it (usernames.UsernameService).
        UsernameService.claim(self.org.username, organization=self.org)
        OrganizationMember.objects.create(
            organization=self.org,
            user=self.user,
            role=OrganizationMember.Role.OWNER,
        )
        self.sport = Sport.objects.create(name="Cricket", icon_name="mdi:cricket")
        self.sport2 = Sport.objects.create(name="Hockey", icon_name="mdi:hockey-sticks")
        self.position = SportPosition.objects.create(sport=self.sport, name="Bowler")

        self.client.force_authenticate(user=self.user)

    # ── helpers ──────────────────────────────────────────────────

    def _org_headers(self):
        return {
            "HTTP_X_ACTOR_TYPE": "organization",
            "HTTP_X_ACTOR_ID": str(self.org.id),
        }

    def _payload(self, **overrides):
        payload = {
            "title": "State Trials",
            "short_description": "Open trials for the state squad.",
            "recruitment_type": "open_trial",
            "sport_id": str(self.sport.id),
            "positions": [],
            "sessions": FUTURE_SESSIONS,
        }
        payload.update(overrides)
        return payload

    def _create(self, **overrides):
        return self.client.post(
            CREATE_URL,
            self._payload(**overrides),
            format="json",
            **self._org_headers(),
        )

    def _update(self, recruitment, **overrides):
        return self.client.patch(
            f"/recruitments/{recruitment.id}/update",
            self._payload(**overrides),
            format="json",
            **self._org_headers(),
        )

    # ── positions ────────────────────────────────────────────────

    def test_create_accepts_empty_positions(self):
        resp = self._create(positions=[])

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        recruitment_id = resp.data["data"]["recruitment_id"]
        self.assertEqual(
            RecruitmentPosition.objects.filter(
                recruitment_id=recruitment_id
            ).count(),
            0,
        )

    # ── draft flow ───────────────────────────────────────────────

    def test_create_draft_is_unpublished_and_hidden(self):
        resp = self._create(status="draft")

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        recruitment_id = resp.data["data"]["recruitment_id"]
        recruitment = Recruitment.objects.get(id=recruitment_id)
        self.assertEqual(recruitment.status, Recruitment.Status.DRAFT)
        self.assertIsNone(recruitment.published_at)

        # detail selector hides drafts from non-owners
        self.assertIsNone(
            RecruitmentSelector.get_recruitment_detail(
                recruitment_id=recruitment_id, actor=None
            )
        )

        # list selector: owner sees the draft, an anonymous viewer does not
        owner_actor = Actor(actor_type="organization", organization=self.org)
        owner_qs, _ = RecruitmentSelector.list_recruitments(
            actor=owner_actor, username=self.org.username
        )
        self.assertIn(recruitment.id, [r.id for r in owner_qs])

        anon_qs, _ = RecruitmentSelector.list_recruitments(
            actor=None, username=self.org.username
        )
        self.assertNotIn(recruitment.id, [r.id for r in anon_qs])

    def test_create_active_sets_published_at(self):
        resp = self._create(status="active")

        recruitment_id = resp.data["data"]["recruitment_id"]
        self.assertIsNotNone(
            Recruitment.objects.get(id=recruitment_id).published_at
        )

    # ── deadline rules ───────────────────────────────────────────

    def test_update_unchanged_past_deadline_succeeds(self):
        past = timezone.now() - timedelta(days=10)
        event = timezone.now() + timedelta(days=20)
        recruitment = Recruitment.objects.create(
            organization=self.org,
            sport=self.sport,
            status=Recruitment.Status.ACTIVE,
            title="Old",
            recruitment_type="open_trial",
            application_deadline=past,
            event_date=event,
        )

        resp = self._update(
            recruitment,
            application_deadline=past.isoformat(),
            event_date=event.isoformat(),
        )

        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_update_move_deadline_to_past_fails(self):
        recruitment = Recruitment.objects.create(
            organization=self.org,
            sport=self.sport,
            status=Recruitment.Status.ACTIVE,
            title="Old",
            recruitment_type="open_trial",
            application_deadline=timezone.now() + timedelta(days=10),
            event_date=timezone.now() + timedelta(days=20),
        )

        resp = self._update(
            recruitment,
            application_deadline=(timezone.now() - timedelta(days=1)).isoformat(),
            event_date=(timezone.now() + timedelta(days=20)).isoformat(),
        )

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            resp.data["message"], "Application deadline cannot be in the past"
        )
        self.assertIn("non_field_errors", resp.data["data"]["errors"])

    # ── applicant-safe edits ─────────────────────────────────────

    def test_update_sport_locked_after_application(self):
        recruitment = Recruitment.objects.create(
            organization=self.org,
            sport=self.sport,
            status=Recruitment.Status.ACTIVE,
            title="Locked",
            recruitment_type="open_trial",
            applications_count=1,
        )

        resp = self._update(
            recruitment, sport_id=str(self.sport2.id), positions=[]
        )

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Sport cannot be changed", resp.data["message"])

    def test_update_max_applications_floor(self):
        recruitment = Recruitment.objects.create(
            organization=self.org,
            sport=self.sport,
            status=Recruitment.Status.ACTIVE,
            title="Cap",
            recruitment_type="open_trial",
            applications_count=5,
        )

        resp = self._update(
            recruitment,
            sport_id=str(self.sport.id),
            positions=[],
            max_applications=3,
        )

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("5", resp.data["message"])

    # ── apply_method hardening ───────────────────────────────────

    def test_create_rejects_invalid_phone_contact(self):
        resp = self._create(
            apply_method="contact",
            contacts=[{"contact_type": "phone", "value": "abc"}],
        )

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("valid phone", resp.data["message"].lower())

    def test_create_clears_external_url_for_non_external(self):
        resp = self._create(
            apply_method="goatza",
            external_apply_url="https://example.com/apply",
        )

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        recruitment_id = resp.data["data"]["recruitment_id"]
        self.assertEqual(
            Recruitment.objects.get(id=recruitment_id).external_apply_url, ""
        )

    # ── structured errors ────────────────────────────────────────

    def test_error_payload_shape(self):
        resp = self._create(is_paid=True)  # fee_amount missing

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIsInstance(resp.data["data"]["errors"], dict)
        self.assertTrue(resp.data["message"])
        self.assertNotIn("ErrorDetail", resp.data["message"])
        self.assertEqual(
            resp.data["message"],
            "fee_amount is required for paid recruitments",
        )

    # ── is_accepting_applications property ───────────────────────

    def test_is_accepting_applications_property(self):
        recruitment = Recruitment.objects.create(
            organization=self.org,
            sport=self.sport,
            status=Recruitment.Status.ACTIVE,
            title="Accepting",
            recruitment_type="open_trial",
        )
        self.assertTrue(recruitment.is_accepting_applications)

        recruitment.status = Recruitment.Status.CLOSED
        self.assertFalse(recruitment.is_accepting_applications)

        recruitment.status = Recruitment.Status.ACTIVE
        recruitment.max_applications = 2
        recruitment.applications_count = 2
        self.assertFalse(recruitment.is_accepting_applications)

        recruitment.applications_count = 1
        self.assertTrue(recruitment.is_accepting_applications)

        recruitment.application_deadline = timezone.now() - timedelta(days=1)
        self.assertFalse(recruitment.is_accepting_applications)

    # ── edit resets: location + fee ──────────────────────────────

    def test_update_clears_location_when_removed(self):
        recruitment = Recruitment.objects.create(
            organization=self.org,
            sport=self.sport,
            status=Recruitment.Status.ACTIVE,
            title="Located",
            recruitment_type="open_trial",
            location_name="Old Ground",
            city="Kannur",
            country_code="IN",
            latitude=11.87,
            longitude=75.37,
        )

        # _payload() carries no `location` → the block is treated as removed
        resp = self._update(recruitment)

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        recruitment.refresh_from_db()
        self.assertEqual(recruitment.location_name, "")
        self.assertEqual(recruitment.city, "")
        self.assertEqual(recruitment.country_code, "")
        self.assertIsNone(recruitment.latitude)
        self.assertIsNone(recruitment.longitude)

    def test_update_paid_to_free_resets_fee(self):
        recruitment = Recruitment.objects.create(
            organization=self.org,
            sport=self.sport,
            status=Recruitment.Status.ACTIVE,
            title="Paid",
            recruitment_type="open_trial",
            is_paid=True,
            fee_amount=Decimal("300.00"),
            fee_currency="USD",
            payment_note="Pay at the gate",
        )

        resp = self._update(recruitment, is_paid=False)

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        recruitment.refresh_from_db()
        self.assertFalse(recruitment.is_paid)
        self.assertIsNone(recruitment.fee_amount)  # fee constraint must not 500
        self.assertEqual(recruitment.payment_note, "")
        self.assertEqual(recruitment.fee_currency, "INR")

    # ── draft visibility to a follower ───────────────────────────

    def test_follower_cannot_see_draft_by_id(self):
        # A followers-only DRAFT: the follower has visibility rights, but the
        # draft status must still hide it everywhere except to the owner.
        draft = Recruitment.objects.create(
            organization=self.org,
            sport=self.sport,
            status=Recruitment.Status.DRAFT,
            visibility=Recruitment.Visibility.FOLLOWERS_ONLY,
            title="Secret Draft",
            recruitment_type="open_trial",
        )
        follower = User.objects.create_user(
            email="fan@example.com", password="pass1234", username="fan"
        )
        accept_current_terms(follower)
        Follow.objects.create(follower_user=follower, following_org=self.org)
        follower_actor = Actor(actor_type="user", user=follower)

        # hitting it directly by id → not found for the follower
        self.assertIsNone(
            RecruitmentSelector.get_recruitment_detail(
                recruitment_id=draft.id, actor=follower_actor
            )
        )
        # and it never shows up in their list
        qs, _ = RecruitmentSelector.list_recruitments(
            actor=follower_actor, username=self.org.username
        )
        self.assertNotIn(draft.id, [r.id for r in qs])


class RecruitmentListOrderingTests(APITestCase):
    """
    The plain list is ordered by WHAT and WHEN, not by when it was posted.

    ``-published_at`` put a June posting for October above last week's posting
    for this Saturday, and let a finished trial outrank a live one. See
    ``RecruitmentSelector.order_for_list``.
    """

    def setUp(self):
        cache.clear()
        self.owner = User.objects.create_user(
            email="ord_o@example.com", password="pass1234", username="ord_owner",
        )
        accept_current_terms(self.owner)
        self.org = Organization.objects.create(
            name="Order FC", username="orderfc", type=Organization.Type.CLUB,
        )
        # The username only resolves once UsernameRegistry holds it.
        UsernameService.claim(self.org.username, organization=self.org)
        OrganizationMember.objects.create(
            organization=self.org, user=self.owner,
            role=OrganizationMember.Role.OWNER,
        )
        self.sport = Sport.objects.create(name="Football", icon_name="mdi:soccer")
        self.owner_actor = Actor(
            actor_type="organization", user=self.owner, organization=self.org,
        )
        self.now = timezone.now()

    def _trial(self, title, *, published_days_ago, event_in_days=None,
               status_value=Recruitment.Status.ACTIVE, deadline=None):
        """One posting. ``event_in_days`` may be negative — a finished trial."""
        event_date = trial_end = None
        if event_in_days is not None:
            event_date = self.now + timedelta(days=event_in_days)
            # What _sync_trial_window would have written for a one-day trial.
            trial_end = event_date
        return Recruitment.objects.create(
            organization=self.org, sport=self.sport,
            status=status_value,
            visibility=Recruitment.Visibility.PUBLIC,
            title=title, recruitment_type="open_trial",
            published_at=self.now - timedelta(days=published_days_ago),
            event_date=event_date,
            trial_end_date=trial_end,
            application_deadline=deadline,
        )

    def _titles(self, actor=None):
        queryset, _ = RecruitmentSelector.list_recruitments(
            actor=actor if actor is not None else self.owner_actor,
            username=self.org.username,
            limit=50,
        )
        return [row.title for row in queryset]

    def test_an_old_posting_for_a_near_trial_outranks_a_new_one_for_a_far_trial(self):
        # The bug, stated: posted in June for October vs posted yesterday for
        # this Saturday. The reader wants Saturday first.
        self._trial("October trial", published_days_ago=120, event_in_days=100)
        self._trial("Saturday trial", published_days_ago=1, event_in_days=3)

        self.assertEqual(
            self._titles(), ["Saturday trial", "October trial"],
        )

    def test_a_finished_trial_sinks_below_every_live_one_however_recent(self):
        # Published today, but its day has been and gone.
        self._trial("Finished yesterday", published_days_ago=0, event_in_days=-1)
        # Published long ago, still to happen.
        self._trial("Still to come", published_days_ago=200, event_in_days=60)
        # Active, trial ahead, but applications have closed: bucket 2, so it
        # sits under the accepting one and above anything finished.
        self._trial(
            "Closed to applications", published_days_ago=200,
            event_in_days=30,
            deadline=self.now - timedelta(days=1),
        )

        self.assertEqual(
            self._titles(),
            ["Still to come", "Closed to applications", "Finished yesterday"],
        )

    def test_finished_trials_come_back_most_recent_first(self):
        # The two-sort-column trap: buckets 1 and 2 sort ASCENDING and buckets
        # 0 and 3 DESCENDING. One shared key would hand these back oldest-first.
        self._trial("Ended long ago", published_days_ago=300, event_in_days=-200)
        self._trial("Ended last week", published_days_ago=300, event_in_days=-7)
        self._trial("Ended yesterday", published_days_ago=300, event_in_days=-1)

        self.assertEqual(
            self._titles(),
            ["Ended yesterday", "Ended last week", "Ended long ago"],
        )


class RecruitmentApplicationLifecycleTests(APITestCase):
    """Withdraw + reapply, org bulk/single status changes, and the player
    status-change notifications."""

    def setUp(self):
        cache.clear()
        self.owner = User.objects.create_user(
            email="own_l@example.com", password="pass1234", username="owner_l"
        )
        accept_current_terms(self.owner)
        self.org = Organization.objects.create(
            name="Lion FC", username="lionfc", type=Organization.Type.CLUB
        )
        self.member = OrganizationMember.objects.create(
            organization=self.org, user=self.owner,
            role=OrganizationMember.Role.OWNER,
        )
        self.player = User.objects.create_user(
            email="p1_l@example.com", password="pass1234", username="player1_l"
        )
        accept_current_terms(self.player)
        self.other = User.objects.create_user(
            email="p2_l@example.com", password="pass1234", username="player2_l"
        )
        accept_current_terms(self.other)
        self.sport = Sport.objects.create(name="Football", icon_name="mdi:soccer")
        self.recruitment = Recruitment.objects.create(
            organization=self.org, sport=self.sport,
            status=Recruitment.Status.ACTIVE, title="U17 Trials",
            recruitment_type="open_trial", apply_method="goatza",
            visibility=Recruitment.Visibility.PUBLIC,
        )

    # ── helpers ──────────────────────────────────────────────────

    def _org_headers(self, org=None):
        return {
            "HTTP_X_ACTOR_TYPE": "organization",
            "HTTP_X_ACTOR_ID": str((org or self.org).id),
        }

    def _make_app(self, applicant, status="applied"):
        return RecruitmentApplication.objects.create(
            recruitment=self.recruitment, applicant=applicant,
            shared_name="Name", shared_phone="+919876543210", status=status,
        )

    def _apply_payload(self, answers=None):
        payload = {
            "shared_name": "Player One",
            "shared_phone": "+919876543210",
            "shared_email": "p1@example.com",
        }
        if answers is not None:
            payload["answers"] = answers
        return payload

    def _apply(self, answers=None):
        self.client.force_authenticate(user=self.player)
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(
                f"/recruitments/{self.recruitment.id}/apply",
                self._apply_payload(answers), format="json",
            )

    def _withdraw(self, application_id, user=None):
        self.client.force_authenticate(user=user or self.player)
        return self.client.post(
            f"/recruitments/applications/{application_id}/withdraw"
        )

    def _bulk_url(self, recruitment=None):
        rid = (recruitment or self.recruitment).id
        return f"/recruitments/{rid}/applications/bulk-status"

    # ── WITHDRAW ─────────────────────────────────────────────────

    def test_withdraw_success_decrements_and_logs_history(self):
        app = self._make_app(self.player, status="shortlisted")
        Recruitment.objects.filter(id=self.recruitment.id).update(
            applications_count=1
        )

        resp = self._withdraw(app.id)

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        app.refresh_from_db()
        self.assertEqual(app.status, "withdrawn")
        self.recruitment.refresh_from_db()
        self.assertEqual(self.recruitment.applications_count, 0)
        self.assertTrue(
            RecruitmentApplicationStatusHistory.objects.filter(
                application=app, from_status="shortlisted",
                to_status="withdrawn", note="Withdrawn by applicant",
            ).exists()
        )

    def test_withdraw_other_players_application_404(self):
        app = self._make_app(self.other, status="applied")
        resp = self._withdraw(app.id)  # acting as self.player
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_withdraw_missing_application_404(self):
        resp = self._withdraw(uuid.uuid4())
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_double_withdraw_400(self):
        app = self._make_app(self.player, status="withdrawn")
        resp = self._withdraw(app.id)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("already withdrawn", resp.data["message"].lower())

    def test_withdraw_counter_floors_at_zero(self):
        app = self._make_app(self.player, status="applied")
        # applications_count is already 0 — must not go negative.
        resp = self._withdraw(app.id)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.recruitment.refresh_from_db()
        self.assertEqual(self.recruitment.applications_count, 0)

    # ── REAPPLY (via the apply endpoint) ─────────────────────────

    def test_reapply_reuses_row_and_reincrements(self):
        r1 = self._apply()
        self.assertEqual(r1.status_code, status.HTTP_200_OK)
        app_id = r1.data["data"]["application_id"]
        self.recruitment.refresh_from_db()
        self.assertEqual(self.recruitment.applications_count, 1)

        self.assertEqual(self._withdraw(app_id).status_code, status.HTTP_200_OK)
        self.recruitment.refresh_from_db()
        self.assertEqual(self.recruitment.applications_count, 0)

        r2 = self._apply()
        self.assertEqual(r2.status_code, status.HTTP_200_OK)
        # SAME row reused, not a second application.
        self.assertEqual(r2.data["data"]["application_id"], app_id)
        self.assertEqual(
            RecruitmentApplication.objects.filter(
                recruitment=self.recruitment, applicant=self.player
            ).count(),
            1,
        )
        app = RecruitmentApplication.objects.get(id=app_id)
        self.assertEqual(app.status, "applied")
        self.recruitment.refresh_from_db()
        self.assertEqual(self.recruitment.applications_count, 1)

    def test_reapply_replaces_answers_resets_review_restamps_applied_at(self):
        question = RecruitmentQuestion.objects.create(
            recruitment=self.recruitment, question="City?",
            field_type="short_text", is_required=False, display_order=0,
        )
        r1 = self._apply(
            answers=[{"question_id": str(question.id), "answer_text": "Kannur"}]
        )
        app_id = r1.data["data"]["application_id"]

        # Simulate an org review before the player withdraws.
        RecruitmentApplication.objects.filter(id=app_id).update(
            reviewed_by=self.member, reviewed_at=timezone.now(),
        )
        self._withdraw(app_id)
        # Push applied_at into the past so the re-stamp is unambiguous.
        RecruitmentApplication.objects.filter(id=app_id).update(
            applied_at=timezone.now() - timedelta(days=1)
        )

        r2 = self._apply(
            answers=[{"question_id": str(question.id), "answer_text": "Kochi"}]
        )
        self.assertEqual(r2.status_code, status.HTTP_200_OK)

        app = RecruitmentApplication.objects.get(id=app_id)
        self.assertEqual(app.status, "applied")
        self.assertIsNone(app.reviewed_by)
        self.assertIsNone(app.reviewed_at)
        self.assertGreater(app.applied_at, timezone.now() - timedelta(minutes=1))

        # Answers replaced wholesale — only the new one remains.
        texts = list(
            RecruitmentApplicationAnswer.objects
            .filter(application=app)
            .values_list("answer_text", flat=True)
        )
        self.assertEqual(texts, ["Kochi"])

        self.assertTrue(
            RecruitmentApplicationStatusHistory.objects.filter(
                application=app, from_status="withdrawn",
                to_status="applied", note="Reapplied",
            ).exists()
        )

    def test_reapply_blocked_when_closed(self):
        r1 = self._apply()
        app_id = r1.data["data"]["application_id"]
        self._withdraw(app_id)
        Recruitment.objects.filter(id=self.recruitment.id).update(
            status=Recruitment.Status.CLOSED
        )

        resp = self._apply()
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("not accepting", resp.data["message"].lower())

    def test_reapply_blocked_when_cap_rehit(self):
        r1 = self._apply()
        app_id = r1.data["data"]["application_id"]
        self._withdraw(app_id)
        # Someone else took the only slot while this applicant was withdrawn.
        Recruitment.objects.filter(id=self.recruitment.id).update(
            max_applications=1, applications_count=1
        )

        resp = self._apply()
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("limit", resp.data["message"].lower())

    def test_apply_twice_without_withdraw_blocked(self):
        self.assertEqual(self._apply().status_code, status.HTTP_200_OK)
        resp = self._apply()
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("already applied", resp.data["message"].lower())
        self.assertEqual(
            RecruitmentApplication.objects.filter(
                recruitment=self.recruitment, applicant=self.player
            ).count(),
            1,
        )

    def test_detail_after_withdraw_surfaces_reapply(self):
        # Regression: after withdraw the detail response must let the player
        # reapply — my_application=withdrawn AND can_apply=true AND the
        # apply_method the FE gate reads is present.
        r1 = self._apply()
        app_id = r1.data["data"]["application_id"]
        self.assertEqual(self._withdraw(app_id).status_code, status.HTTP_200_OK)

        self.client.force_authenticate(user=self.player)
        resp = self.client.get(f"/recruitments/{self.recruitment.id}/details")

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.data["data"]
        self.assertIsNotNone(data["my_application"])
        self.assertEqual(data["my_application"]["status"], "withdrawn")
        self.assertTrue(data["can_apply"])
        self.assertEqual(data["apply_method"], "goatza")

    # ── BULK STATUS ──────────────────────────────────────────────

    def test_bulk_status_happy(self):
        a1 = self._make_app(self.player, "applied")
        a2 = self._make_app(self.other, "reviewing")

        self.client.force_authenticate(user=self.owner)
        with self.captureOnCommitCallbacks(execute=True):
            resp = self.client.post(
                self._bulk_url(),
                {"application_ids": [str(a1.id), str(a2.id)], "status": "trial_confirmed"},
                format="json", **self._org_headers(),
            )

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(set(resp.data["data"]["updated"]), {str(a1.id), str(a2.id)})
        self.assertIn("status_counts", resp.data["data"])

        a1.refresh_from_db()
        self.assertEqual(a1.status, "trial_confirmed")
        self.assertEqual(a1.reviewed_by, self.member)
        self.assertIsNotNone(a1.reviewed_at)
        self.assertTrue(
            RecruitmentApplicationStatusHistory.objects.filter(
                application=a1, to_status="trial_confirmed", changed_by=self.member,
            ).exists()
        )
        # One status notification per updated applicant.
        self.assertEqual(
            Notification.objects.filter(
                type="recruitment_application_status", recipient_user=self.player
            ).count(),
            1,
        )
        self.assertEqual(
            Notification.objects.filter(
                type="recruitment_application_status", recipient_user=self.other
            ).count(),
            1,
        )

    def test_bulk_status_partial_results(self):
        a_ok = self._make_app(self.player, "applied")
        a_withdrawn = self._make_app(self.other, "withdrawn")
        third = User.objects.create_user(
            email="p3_l@example.com", password="pass1234", username="player3_l"
        )
        accept_current_terms(third)
        a_nochange = self._make_app(third, "trial_confirmed")
        missing = str(uuid.uuid4())

        self.client.force_authenticate(user=self.owner)
        with self.captureOnCommitCallbacks(execute=True):
            resp = self.client.post(
                self._bulk_url(),
                {
                    "application_ids": [
                        str(a_ok.id), str(a_withdrawn.id),
                        str(a_nochange.id), missing,
                    ],
                    "status": "trial_confirmed",
                },
                format="json", **self._org_headers(),
            )

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.data["data"]
        self.assertEqual(data["updated"], [str(a_ok.id)])
        reasons = {s["id"]: s["reason"] for s in data["skipped"]}
        self.assertEqual(reasons[str(a_withdrawn.id)], "withdrawn")
        self.assertEqual(reasons[str(a_nochange.id)], "no_change")
        self.assertEqual(reasons[missing], "not_found")

        a_withdrawn.refresh_from_db()
        self.assertEqual(a_withdrawn.status, "withdrawn")  # untouched
        # Notification only for the one actually updated.
        self.assertEqual(
            Notification.objects.filter(
                type="recruitment_application_status"
            ).count(),
            1,
        )

    def test_bulk_status_over_100_rejected(self):
        ids = [str(uuid.uuid4()) for _ in range(101)]
        self.client.force_authenticate(user=self.owner)
        resp = self.client.post(
            self._bulk_url(),
            {"application_ids": ids, "status": "shortlisted"},
            format="json", **self._org_headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_bulk_status_invalid_target_rejected(self):
        a = self._make_app(self.player, "applied")
        self.client.force_authenticate(user=self.owner)
        resp = self.client.post(
            self._bulk_url(),
            {"application_ids": [str(a.id)], "status": "withdrawn"},
            format="json", **self._org_headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_bulk_status_cross_org_404(self):
        other_org = Organization.objects.create(
            name="Rival FC", username="rivalfc_l", type=Organization.Type.CLUB
        )
        OrganizationMember.objects.create(
            organization=other_org, user=self.owner,
            role=OrganizationMember.Role.OWNER,
        )
        a = self._make_app(self.player, "applied")

        self.client.force_authenticate(user=self.owner)
        resp = self.client.post(
            self._bulk_url(),  # recruitment belongs to self.org
            {"application_ids": [str(a.id)], "status": "shortlisted"},
            format="json", **self._org_headers(org=other_org),
        )
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    # ── SINGLE STATUS ────────────────────────────────────────────

    def test_single_status_success(self):
        a = self._make_app(self.player, "applied")
        self.client.force_authenticate(user=self.owner)
        with self.captureOnCommitCallbacks(execute=True):
            resp = self.client.post(
                f"/recruitments/applications/{a.id}/status",
                {"status": "trial_confirmed"}, format="json", **self._org_headers(),
            )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        a.refresh_from_db()
        self.assertEqual(a.status, "trial_confirmed")
        self.assertEqual(a.reviewed_by, self.member)
        self.assertEqual(
            Notification.objects.filter(
                type="recruitment_application_status", recipient_user=self.player
            ).count(),
            1,
        )

    def test_single_status_accepts_and_maps_a_legacy_value(self):
        # `invited` was retired by the v3 split, but an installed PWA keeps
        # sending it for days after a deploy. The endpoint ACCEPTS it and
        # translates — refusing would 400 and lose the org's decision.
        #
        # This is the HTTP-level proof: the serializer's ChoiceField has to
        # let the old word through before the service can map it.
        a = self._make_app(self.player, "applied")
        self.client.force_authenticate(user=self.owner)
        resp = self.client.post(
            f"/recruitments/applications/{a.id}/status",
            {"status": "invited"}, format="json", **self._org_headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertEqual(resp.data["data"]["status"], "trial_confirmed")

        a.refresh_from_db()
        self.assertEqual(a.status, "trial_confirmed")

        # Junk is still junk — widening the accepted set did not open it up.
        resp = self.client.post(
            f"/recruitments/applications/{a.id}/status",
            {"status": "nonsense"}, format="json", **self._org_headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_single_status_withdrawn_400(self):
        a = self._make_app(self.player, "withdrawn")
        self.client.force_authenticate(user=self.owner)
        resp = self.client.post(
            f"/recruitments/applications/{a.id}/status",
            {"status": "shortlisted"}, format="json", **self._org_headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("withdrew", resp.data["message"].lower())

    def test_single_status_cross_org_404(self):
        other_org = Organization.objects.create(
            name="Rival Two", username="rival2_l", type=Organization.Type.CLUB
        )
        OrganizationMember.objects.create(
            organization=other_org, user=self.owner,
            role=OrganizationMember.Role.OWNER,
        )
        a = self._make_app(self.player, "applied")
        self.client.force_authenticate(user=self.owner)
        resp = self.client.post(
            f"/recruitments/applications/{a.id}/status",
            {"status": "shortlisted"}, format="json",
            **self._org_headers(org=other_org),
        )
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    # ── STATUS NOTIFICATION payload ──────────────────────────────

    def test_status_notification_data_and_payload_copy(self):
        from apps.notifications.services.notification_service import (
            build_notification_payload,
        )

        a = self._make_app(self.player, "applied")
        self.client.force_authenticate(user=self.owner)
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(
                f"/recruitments/applications/{a.id}/status",
                {"status": "trial_confirmed"}, format="json", **self._org_headers(),
            )

        notif = Notification.objects.get(
            type="recruitment_application_status", recipient_user=self.player
        )
        self.assertEqual(notif.recruitment_id, self.recruitment.id)
        self.assertEqual(str(notif.actor_org_id), str(self.org.id))
        self.assertEqual(notif.data["to_status"], "trial_confirmed")
        self.assertEqual(notif.data["application_id"], str(a.id))

        payload = build_notification_payload(notif)
        self.assertEqual(payload["type"], "recruitment_application_status")
        self.assertIn("confirmed you for the trial", payload["title"])
        self.assertIn("you're confirmed for the trial", payload["body"])
        self.assertEqual(payload["url"], f"/recruitments/{self.recruitment.id}")
        self.assertEqual(payload["recruitment_id"], str(self.recruitment.id))


class RecruitmentDiscoveryTests(APITestCase):
    """Player-facing discovery: the extended list filters (search /
    experience_level / apply_method / birth_year) and the my-applications
    endpoint."""

    LIST_URL = "/recruitments/list"
    MY_APPS_URL = "/recruitments/applications/my"

    def setUp(self):
        cache.clear()  # username→profile lookups are cached
        self.player = User.objects.create_user(
            email="disc_p@example.com", password="pass1234",
            username="disc_player",
        )
        accept_current_terms(self.player)
        self.owner = User.objects.create_user(
            email="disc_o@example.com", password="pass1234",
            username="disc_owner",
        )
        accept_current_terms(self.owner)
        self.org = Organization.objects.create(
            name="Falcon Academy", username="falconacademy",
            type=Organization.Type.CLUB,
        )
        self.member = OrganizationMember.objects.create(
            organization=self.org, user=self.owner,
            role=OrganizationMember.Role.OWNER,
        )
        self.other_org = Organization.objects.create(
            name="United Trials Club", username="unitedtrials",
            type=Organization.Type.CLUB,
        )
        self.sport = Sport.objects.create(name="Football", icon_name="mdi:soccer")
        self.sport2 = Sport.objects.create(
            name="Basketball", icon_name="mdi:basketball"
        )

    # ── helpers ──────────────────────────────────────────────────

    def _make_recruitment(self, org=None, sport=None, **overrides):
        # Active + public so a non-owner player sees it in the list.
        data = dict(
            organization=org or self.org,
            sport=sport or self.sport,
            status=Recruitment.Status.ACTIVE,
            visibility=Recruitment.Visibility.PUBLIC,
            title="Trials",
            recruitment_type="open_trial",
            apply_method="goatza",
        )
        data.update(overrides)
        return Recruitment.objects.create(**data)

    def _make_app(self, recruitment, applicant=None, status="applied"):
        return RecruitmentApplication.objects.create(
            recruitment=recruitment,
            applicant=applicant or self.player,
            shared_name="Name", shared_phone="+919876543210", status=status,
        )

    def _list(self, **params):
        self.client.force_authenticate(user=self.player)
        return self.client.get(self.LIST_URL, params)

    def _ids(self, resp):
        return [str(r["id"]) for r in resp.data["data"]["results"]]

    def _my_apps(self, user=None, org=None, **params):
        self.client.force_authenticate(user=user or self.player)
        headers = {}
        if org is not None:
            headers = {
                "HTTP_X_ACTOR_TYPE": "organization",
                "HTTP_X_ACTOR_ID": str(org.id),
            }
        return self.client.get(self.MY_APPS_URL, params, **headers)

    # ── LIST: search ─────────────────────────────────────────────

    def test_list_search_matches_title_description_and_org_name(self):
        r_title = self._make_recruitment(title="Goalkeeper Wanted")
        r_desc = self._make_recruitment(
            title="Midfield Program",
            short_description="Elite goalkeeper training included",
        )
        r_org = self._make_recruitment(
            org=self.other_org, title="Striker Search"
        )

        # title + short_description both hit on "goalkeeper" (case-insensitive).
        resp = self._list(search="GOALKEEPER")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        ids = self._ids(resp)
        self.assertIn(str(r_title.id), ids)
        self.assertIn(str(r_desc.id), ids)
        self.assertNotIn(str(r_org.id), ids)

        # organization name match.
        resp = self._list(search="united")
        ids = self._ids(resp)
        self.assertEqual(ids, [str(r_org.id)])

    # ── LIST: experience_level ───────────────────────────────────

    def test_list_experience_level_filter_case_insensitive(self):
        r_pro = self._make_recruitment(experience_level="Professional")
        r_am = self._make_recruitment(experience_level="Amateur")

        resp = self._list(experience_level="professional")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        ids = self._ids(resp)
        self.assertEqual(ids, [str(r_pro.id)])
        self.assertNotIn(str(r_am.id), ids)

    # ── LIST: apply_method (junk ignored) ────────────────────────

    def test_list_apply_method_valid_and_junk_ignored(self):
        r_goatza = self._make_recruitment(apply_method="goatza")
        r_ext = self._make_recruitment(
            apply_method="external",
            external_apply_url="https://example.com/apply",
        )

        # honoured value → only external.
        resp = self._list(apply_method="external")
        self.assertEqual(self._ids(resp), [str(r_ext.id)])

        # junk value → ignored, both returned.
        resp = self._list(apply_method="not-a-method")
        ids = self._ids(resp)
        self.assertIn(str(r_goatza.id), ids)
        self.assertIn(str(r_ext.id), ids)

    # ── LIST: birth_year ─────────────────────────────────────────

    def test_list_birth_year_inside_and_outside_range(self):
        r = self._make_recruitment()
        RecruitmentAgeCategory.objects.create(
            recruitment=r, title="U15",
            min_birth_year=2008, max_birth_year=2010,
        )

        # inside the range → matched.
        resp = self._list(birth_year=2009)
        self.assertEqual(self._ids(resp), [str(r.id)])

        # boundary years are inclusive.
        self.assertEqual(self._ids(self._list(birth_year=2008)), [str(r.id)])
        self.assertEqual(self._ids(self._list(birth_year=2010)), [str(r.id)])

        # outside the range → excluded.
        resp = self._list(birth_year=2005)
        self.assertEqual(resp.data["data"]["count"], 0)
        self.assertEqual(self._ids(resp), [])

        # non-integer junk → filter ignored entirely, recruitment still listed.
        resp = self._list(birth_year="abc")
        self.assertIn(str(r.id), self._ids(resp))

    def test_list_birth_year_distinct_with_multiple_matching_categories(self):
        r = self._make_recruitment()
        # Two categories BOTH containing 2008 — the join would duplicate the
        # recruitment row without .distinct().
        RecruitmentAgeCategory.objects.create(
            recruitment=r, title="U15",
            min_birth_year=2005, max_birth_year=2010,
        )
        RecruitmentAgeCategory.objects.create(
            recruitment=r, title="Open",
            min_birth_year=2000, max_birth_year=2015,
        )

        resp = self._list(birth_year=2008)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["data"]["count"], 1)
        self.assertEqual(self._ids(resp), [str(r.id)])

    # ── MY APPLICATIONS ──────────────────────────────────────────

    def test_my_applications_happy_path(self):
        r1 = self._make_recruitment(title="Keeper Trials")
        r2 = self._make_recruitment(sport=self.sport2, title="Guard Trials")
        app1 = self._make_app(r1)
        app2 = self._make_app(r2)
        # Another player's application must never leak into this player's list.
        other = User.objects.create_user(
            email="disc_x@example.com", password="pass1234", username="disc_x"
        )
        accept_current_terms(other)
        self._make_app(r1, applicant=other)

        resp = self._my_apps()

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.data["data"]
        self.assertEqual(data["count"], 2)
        returned_ids = {str(r["id"]) for r in data["results"]}
        self.assertEqual(returned_ids, {str(app1.id), str(app2.id)})

        # nested recruitment summary shape.
        row = next(
            r for r in data["results"] if str(r["id"]) == str(app1.id)
        )
        recruitment = row["recruitment"]
        self.assertEqual(str(recruitment["id"]), str(r1.id))
        self.assertEqual(recruitment["title"], "Keeper Trials")
        for key in (
            "recruitment_type", "status", "city",
            "event_date", "application_deadline",
        ):
            self.assertIn(key, recruitment)
        self.assertEqual(
            set(recruitment["organization"].keys()),
            {"id", "name", "username", "logo", "is_verified"},
        )
        self.assertEqual(
            set(recruitment["sport"].keys()),
            {"id", "name", "icon_name", "icon_url"},
        )

    def test_my_applications_status_filter(self):
        r = self._make_recruitment()
        r2 = self._make_recruitment(title="Second")
        applied = self._make_app(r, status="applied")
        shortlisted = self._make_app(r2, status="shortlisted")

        resp = self._my_apps(status="shortlisted")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["data"]["count"], 1)
        self.assertEqual(self._ids(resp), [str(shortlisted.id)])

        # junk status → ignored (lenient), both returned.
        resp = self._my_apps(status="not-a-status")
        self.assertEqual(resp.data["data"]["count"], 2)

    def test_my_applications_pagination(self):
        for i in range(3):
            r = self._make_recruitment(title=f"R{i}")
            self._make_app(r)

        resp = self._my_apps(limit=1, offset=0)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.data["data"]
        self.assertEqual(data["count"], 3)
        self.assertEqual(data["limit"], 1)
        self.assertEqual(len(data["results"]), 1)

    def test_my_applications_org_actor_rejected(self):
        # Acting as the org (owner is a verified member) → not a player actor.
        resp = self._my_apps(user=self.owner, org=self.org)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_my_applications_withdrawn_still_listed_with_status(self):
        r = self._make_recruitment()
        withdrawn = self._make_app(r, status="withdrawn")

        resp = self._my_apps()
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["data"]["count"], 1)
        row = resp.data["data"]["results"][0]
        self.assertEqual(str(row["id"]), str(withdrawn.id))
        self.assertEqual(row["status"], "withdrawn")


class RecruitmentEligibilityTests(APITestCase):
    """Recruiter-authored eligibility: open-ended age groups, the age-category
    diff sync (applications must survive an org edit), the group an applicant
    applies under, and the free-text criteria lines.

    Nothing here enforces eligibility — the platform only records and displays
    what the recruiter wrote and what the applicant picked."""

    def setUp(self):
        cache.clear()  # username→profile lookups are cached
        self.owner = User.objects.create_user(
            email="elig_o@example.com", password="pass1234",
            username="elig_owner",
        )
        accept_current_terms(self.owner)
        self.org = Organization.objects.create(
            name="Eagle FC", username="eaglefc", type=Organization.Type.CLUB,
        )
        OrganizationMember.objects.create(
            organization=self.org, user=self.owner,
            role=OrganizationMember.Role.OWNER,
        )
        self.player = User.objects.create_user(
            email="elig_p@example.com", password="pass1234",
            username="elig_player",
        )
        accept_current_terms(self.player)
        self.other_player = User.objects.create_user(
            email="elig_p2@example.com", password="pass1234",
            username="elig_player2",
        )
        accept_current_terms(self.other_player)
        self.sport = Sport.objects.create(name="Football", icon_name="mdi:soccer")

    # ── helpers ──────────────────────────────────────────────────

    def _org_headers(self):
        return {
            "HTTP_X_ACTOR_TYPE": "organization",
            "HTTP_X_ACTOR_ID": str(self.org.id),
        }

    def _payload(self, **overrides):
        payload = {
            "title": "Academy Trials",
            "recruitment_type": "open_trial",
            "sport_id": str(self.sport.id),
            "positions": [],
            "sessions": FUTURE_SESSIONS,
        }
        payload.update(overrides)
        return payload

    def _create(self, **overrides):
        self.client.force_authenticate(user=self.owner)
        return self.client.post(
            CREATE_URL, self._payload(**overrides),
            format="json", **self._org_headers(),
        )

    def _create_recruitment(self, **overrides):
        """Create via the API and return the Recruitment (asserts success)."""
        resp = self._create(**overrides)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        return Recruitment.objects.get(
            id=resp.data["data"]["recruitment_id"]
        )

    def _update(self, recruitment, **overrides):
        self.client.force_authenticate(user=self.owner)
        return self.client.patch(
            f"/recruitments/{recruitment.id}/update",
            self._payload(**overrides),
            format="json", **self._org_headers(),
        )

    def _detail(self, recruitment):
        self.client.force_authenticate(user=self.owner)
        return self.client.get(
            f"/recruitments/{recruitment.id}/details", **self._org_headers()
        )

    def _groups(self, recruitment):
        return list(recruitment.age_categories.order_by("display_order"))

    def _group_payload(self, category, **overrides):
        """Round-trip an existing group back as the client would on edit —
        carrying its id so the diff sync updates it in place."""
        data = {
            "id": str(category.id),
            "title": category.title,
            "min_birth_year": category.min_birth_year,
            "max_birth_year": category.max_birth_year,
            "display_order": category.display_order,
        }
        if category.reporting_time:
            data["reporting_time"] = category.reporting_time.isoformat()
        data.update(overrides)
        return data

    def _apply(self, recruitment, user=None, **extra):
        payload = {
            "shared_name": "Player One",
            "shared_phone": "+919876543210",
        }
        payload.update(extra)
        self.client.force_authenticate(user=user or self.player)
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(
                f"/recruitments/{recruitment.id}/apply",
                payload, format="json",
            )

    # ── AGE GROUP VALIDATION ─────────────────────────────────────

    def test_create_rejects_age_group_with_no_years(self):
        # Both bounds empty is not "all ages" — all ages is an EMPTY list.
        resp = self._create(
            age_categories=[{"title": "Anyone"}]
        )

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("minimum or a maximum", resp.data["message"])
        self.assertFalse(RecruitmentAgeCategory.objects.exists())

    def test_create_accepts_min_only_age_group(self):
        recruitment = self._create_recruitment(
            age_categories=[
                {"title": "U17", "min_birth_year": 2010}
            ]
        )

        group = recruitment.age_categories.get()
        self.assertEqual(group.min_birth_year, 2010)
        self.assertIsNone(group.max_birth_year)

    def test_create_accepts_max_only_age_group(self):
        recruitment = self._create_recruitment(
            age_categories=[
                {"title": "Veterans 35+", "max_birth_year": 1991}
            ]
        )

        group = recruitment.age_categories.get()
        self.assertIsNone(group.min_birth_year)
        self.assertEqual(group.max_birth_year, 1991)

    def test_create_rejects_inverted_birth_year_range(self):
        resp = self._create(
            age_categories=[
                {
                    "title": "Backwards",
                    "min_birth_year": 2012,
                    "max_birth_year": 2010,
                }
            ]
        )

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Invalid birth year range", resp.data["message"])

    def test_create_rejects_birth_year_below_1950(self):
        resp = self._create(
            age_categories=[
                {"title": "Ancient", "max_birth_year": 1949}
            ]
        )

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("1950", resp.data["message"])

    def test_db_constraint_rejects_age_group_with_no_years(self):
        # The serializer is not the only gate — bulk_create skips model
        # validation, so the constraint has to hold at the DB level too.
        recruitment = self._create_recruitment()

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                RecruitmentAgeCategory.objects.create(
                    recruitment=recruitment, title="Broken",
                )

    def test_detail_exposes_open_ended_age_groups(self):
        recruitment = self._create_recruitment(
            age_categories=[
                {
                    "title": "U15",
                    "min_birth_year": 2011,
                    "max_birth_year": 2012,
                    "display_order": 0,
                },
                {"title": "U17", "min_birth_year": 2010, "display_order": 1},
            ]
        )

        resp = self._detail(recruitment)

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        groups = resp.data["data"]["age_categories"]
        self.assertEqual([g["title"] for g in groups], ["U15", "U17"])
        self.assertEqual(groups[1]["min_birth_year"], 2010)
        self.assertIsNone(groups[1]["max_birth_year"])

    # ── AGE GROUP DIFF SYNC ──────────────────────────────────────

    def test_update_with_same_ids_preserves_rows_and_applications(self):
        # THE regression this whole diff sync exists for: an org renaming a
        # group must not wipe the group every applicant applied under.
        recruitment = self._create_recruitment(
            age_categories=[
                {
                    "title": "U15",
                    "min_birth_year": 2011,
                    "max_birth_year": 2012,
                    "display_order": 0,
                },
                {"title": "U17", "min_birth_year": 2010, "display_order": 1},
            ]
        )
        u15, u17 = self._groups(recruitment)

        apply_resp = self._apply(recruitment, age_category=str(u17.id))
        self.assertEqual(apply_resp.status_code, status.HTTP_200_OK)
        application = RecruitmentApplication.objects.get(
            id=apply_resp.data["data"]["application_id"]
        )
        self.assertEqual(application.age_category_id, u17.id)

        resp = self._update(
            recruitment,
            age_categories=[
                self._group_payload(u15),
                self._group_payload(u17, title="U17 Boys"),
            ],
        )

        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        # Same rows, updated in place — not recreated.
        self.assertEqual(
            {g.id for g in self._groups(recruitment)}, {u15.id, u17.id}
        )
        u17.refresh_from_db()
        self.assertEqual(u17.title, "U17 Boys")
        # ...and the applicant is still in their group.
        application.refresh_from_db()
        self.assertEqual(application.age_category_id, u17.id)

    def test_update_deletes_dropped_group_and_nulls_its_applications(self):
        recruitment = self._create_recruitment(
            age_categories=[
                {"title": "U15", "min_birth_year": 2011, "display_order": 0},
                {"title": "U17", "min_birth_year": 2010, "display_order": 1},
            ]
        )
        u15, u17 = self._groups(recruitment)
        apply_resp = self._apply(recruitment, age_category=str(u15.id))
        application = RecruitmentApplication.objects.get(
            id=apply_resp.data["data"]["application_id"]
        )

        # U15 dropped from the payload → deleted; U17 kept.
        resp = self._update(
            recruitment, age_categories=[self._group_payload(u17)]
        )

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual([g.id for g in self._groups(recruitment)], [u17.id])
        self.assertFalse(
            RecruitmentAgeCategory.objects.filter(id=u15.id).exists()
        )
        # SET_NULL, not a cascade — the application survives without a group.
        application.refresh_from_db()
        self.assertIsNone(application.age_category_id)

    def test_update_adds_new_group_alongside_existing_ones(self):
        recruitment = self._create_recruitment(
            age_categories=[
                {"title": "U15", "min_birth_year": 2011, "display_order": 0}
            ]
        )
        u15 = self._groups(recruitment)[0]

        resp = self._update(
            recruitment,
            age_categories=[
                self._group_payload(u15),
                {
                    "title": "Veterans 35+",
                    "max_birth_year": 1991,
                    "display_order": 1,
                },
            ],
        )

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        groups = self._groups(recruitment)
        self.assertEqual([g.title for g in groups], ["U15", "Veterans 35+"])
        self.assertEqual(groups[0].id, u15.id)  # untouched
        self.assertIsNone(groups[1].min_birth_year)

    def test_update_rejects_age_group_id_from_another_recruitment(self):
        recruitment = self._create_recruitment(
            age_categories=[
                {"title": "U15", "min_birth_year": 2011, "display_order": 0}
            ]
        )
        other = self._create_recruitment(
            title="Other Trials",
            age_categories=[
                {"title": "Foreign", "min_birth_year": 2000, "display_order": 0}
            ],
        )
        foreign_group = self._groups(other)[0]

        resp = self._update(
            recruitment,
            age_categories=[
                self._group_payload(foreign_group, title="Stolen")
            ],
        )

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Invalid age category id", resp.data["message"])
        # Nothing moved: the foreign group still belongs to the other
        # recruitment, under its original name.
        foreign_group.refresh_from_db()
        self.assertEqual(foreign_group.recruitment_id, other.id)
        self.assertEqual(foreign_group.title, "Foreign")
        self.assertEqual(
            [g.title for g in self._groups(recruitment)], ["U15"]
        )

    def test_update_all_ages_clears_every_group(self):
        # "Open to all ages" is submitted as an empty list, not a flag.
        recruitment = self._create_recruitment(
            age_categories=[
                {"title": "U15", "min_birth_year": 2011, "display_order": 0}
            ]
        )

        resp = self._update(recruitment, age_categories=[])

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(self._groups(recruitment), [])

    # ── APPLYING UNDER A GROUP ───────────────────────────────────

    def test_apply_with_group_stores_and_surfaces_it(self):
        recruitment = self._create_recruitment(
            age_categories=[
                {
                    "title": "U17",
                    "min_birth_year": 2010,
                    "reporting_time": "09:00:00",
                    "display_order": 0,
                }
            ]
        )
        group = self._groups(recruitment)[0]

        resp = self._apply(recruitment, age_category=str(group.id))

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        application = RecruitmentApplication.objects.get(
            id=resp.data["data"]["application_id"]
        )
        self.assertEqual(application.age_category_id, group.id)

        # the player sees their own group + its reporting time on the detail
        self.client.force_authenticate(user=self.player)
        detail = self.client.get(f"/recruitments/{recruitment.id}/details")
        mine = detail.data["data"]["my_application"]
        self.assertEqual(mine["age_category"]["title"], "U17")
        self.assertEqual(mine["age_category"]["reporting_time"], "09:00:00")

        # and so does the org, on its applicants list
        self.client.force_authenticate(user=self.owner)
        listing = self.client.get(
            f"/recruitments/{recruitment.id}/applications",
            **self._org_headers(),
        )
        row = listing.data["data"]["results"][0]
        self.assertEqual(row["age_category"]["title"], "U17")

    def test_apply_rejects_group_from_another_recruitment(self):
        recruitment = self._create_recruitment(
            age_categories=[
                {"title": "U17", "min_birth_year": 2010, "display_order": 0}
            ]
        )
        other = self._create_recruitment(
            title="Other Trials",
            age_categories=[
                {"title": "U19", "min_birth_year": 2008, "display_order": 0}
            ],
        )
        foreign_group = self._groups(other)[0]

        resp = self._apply(recruitment, age_category=str(foreign_group.id))

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Invalid age group", resp.data["message"])
        self.assertFalse(
            RecruitmentApplication.objects.filter(
                recruitment=recruitment
            ).exists()
        )

    def test_apply_without_group_still_works(self):
        # Optional at the API level — older clients and all-ages recruitments.
        recruitment = self._create_recruitment(
            age_categories=[
                {"title": "U17", "min_birth_year": 2010, "display_order": 0}
            ]
        )

        resp = self._apply(recruitment)

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        application = RecruitmentApplication.objects.get(
            id=resp.data["data"]["application_id"]
        )
        self.assertIsNone(application.age_category_id)

    def test_reapply_updates_the_chosen_group(self):
        recruitment = self._create_recruitment(
            age_categories=[
                {"title": "U15", "min_birth_year": 2011, "display_order": 0},
                {"title": "U17", "min_birth_year": 2010, "display_order": 1},
            ]
        )
        u15, u17 = self._groups(recruitment)

        first = self._apply(recruitment, age_category=str(u15.id))
        application_id = first.data["data"]["application_id"]

        self.client.force_authenticate(user=self.player)
        self.client.post(
            f"/recruitments/applications/{application_id}/withdraw"
        )

        second = self._apply(recruitment, age_category=str(u17.id))

        self.assertEqual(second.status_code, status.HTTP_200_OK)
        # same revived row, new group
        self.assertEqual(
            second.data["data"]["application_id"], application_id
        )
        application = RecruitmentApplication.objects.get(id=application_id)
        self.assertEqual(application.age_category_id, u17.id)

    def test_org_applicants_list_filters_by_group(self):
        recruitment = self._create_recruitment(
            age_categories=[
                {"title": "U15", "min_birth_year": 2011, "display_order": 0},
                {"title": "U17", "min_birth_year": 2010, "display_order": 1},
            ]
        )
        u15, u17 = self._groups(recruitment)
        in_u15 = RecruitmentApplication.objects.create(
            recruitment=recruitment, applicant=self.player,
            shared_name="A", shared_phone="+919876543210", age_category=u15,
        )
        in_u17 = RecruitmentApplication.objects.create(
            recruitment=recruitment, applicant=self.other_player,
            shared_name="B", shared_phone="+919876543211", age_category=u17,
        )

        self.client.force_authenticate(user=self.owner)
        url = f"/recruitments/{recruitment.id}/applications"

        resp = self.client.get(
            url, {"age_category": str(u17.id)}, **self._org_headers()
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [str(r["id"]) for r in resp.data["data"]["results"]],
            [str(in_u17.id)],
        )

        # junk → filter ignored (lenient), never a 500
        resp = self.client.get(
            url, {"age_category": "not-a-uuid"}, **self._org_headers()
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(
            {str(r["id"]) for r in resp.data["data"]["results"]},
            {str(in_u15.id), str(in_u17.id)},
        )

    # ── DISCOVERY: birth_year vs open-ended groups ───────────────

    def test_list_birth_year_matches_open_ended_groups(self):
        min_only = self._create_recruitment(
            title="U17 Trials",
            age_categories=[
                {"title": "U17", "min_birth_year": 2010, "display_order": 0}
            ],
        )
        max_only = self._create_recruitment(
            title="Veterans Trials",
            age_categories=[
                {
                    "title": "Veterans 35+",
                    "max_birth_year": 1991,
                    "display_order": 0,
                }
            ],
        )

        self.client.force_authenticate(user=self.player)

        def ids(birth_year):
            resp = self.client.get("/recruitments/list", {"birth_year": birth_year})
            self.assertEqual(resp.status_code, status.HTTP_200_OK)
            return {str(r["id"]) for r in resp.data["data"]["results"]}

        # "born 2010 or later" — a null max must not exclude a later year.
        self.assertEqual(ids(2012), {str(min_only.id)})
        # "born 1991 or earlier" — likewise for a null min.
        self.assertEqual(ids(1980), {str(max_only.id)})
        # a year outside both still matches neither.
        self.assertEqual(ids(2000), set())

    # ── ELIGIBILITY CRITERIA ─────────────────────────────────────

    def test_eligibility_criteria_create_update_round_trip(self):
        recruitment = self._create_recruitment(
            eligibility_criteria=[
                {"title": "Kerala residents only", "display_order": 0},
                {
                    "title": "District-level experience required",
                    "display_order": 1,
                },
            ]
        )

        resp = self._detail(recruitment)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [c["title"] for c in resp.data["data"]["eligibility_criteria"]],
            ["Kerala residents only", "District-level experience required"],
        )

        # replace-on-update: the old lines go, the new ones keep their order
        update = self._update(
            recruitment,
            eligibility_criteria=[
                {"title": "Own boots required", "display_order": 0},
                {"title": "Aadhaar card at the venue", "display_order": 1},
            ],
        )
        self.assertEqual(update.status_code, status.HTTP_200_OK)
        self.assertEqual(
            list(
                recruitment.eligibility_criteria
                .order_by("display_order")
                .values_list("title", flat=True)
            ),
            ["Own boots required", "Aadhaar card at the venue"],
        )
        self.assertEqual(
            RecruitmentEligibilityCriteria.objects.filter(
                recruitment=recruitment
            ).count(),
            2,
        )

    def test_update_clears_eligibility_criteria_when_omitted(self):
        recruitment = self._create_recruitment(
            eligibility_criteria=[{"title": "Kerala residents only"}]
        )

        resp = self._update(recruitment)  # payload carries no criteria

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(recruitment.eligibility_criteria.count(), 0)

    def test_all_ages_recruitment_has_no_groups_or_criteria(self):
        # The all-ages path end to end: no groups, no criteria, and the detail
        # payload says so with empty lists rather than anything special.
        recruitment = self._create_recruitment()

        resp = self._detail(recruitment)

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["data"]["age_categories"], [])
        self.assertEqual(resp.data["data"]["eligibility_criteria"], [])


# =====================================================================
# DISCOVERY & RANKING (Goatza_Recruitment_Discovery_Spec §2–§4)
# =====================================================================

# Thiruvananthapuram — the worked example's player location (§3).
TVM_LAT = 8.5241
TVM_LNG = 76.9366

# Degrees of latitude per km, matching the bounding-box helper's constant.
# Offsetting purely in latitude keeps the fixtures readable and lands within
# ~0.2% of the target km, which is nowhere near a band boundary.
DEG_PER_KM = 1 / 111.0


class RecruitmentEligibilityEngineTests(APITestCase):
    """
    §2 player-fit eligibility. Display + ranking only — nothing here is allowed
    to reach the apply flow (see RecruitmentApplyFlowIsolationTests).
    """

    def setUp(self):
        self.org = Organization.objects.create(
            name="Engine FC", username="enginefc",
            type=Organization.Type.CLUB,
        )
        self.sport = Sport.objects.create(name="Football", icon_name="mdi:soccer")

    def _recruitment(self, **overrides):
        data = dict(
            organization=self.org,
            sport=self.sport,
            status=Recruitment.Status.ACTIVE,
            visibility=Recruitment.Visibility.PUBLIC,
            title="Trials",
            recruitment_type="open_trial",
        )
        data.update(overrides)
        return Recruitment.objects.create(**data)

    def _verdict(self, recruitment, **context_kwargs):
        return eligibility_service.evaluate(
            recruitment, PlayerContext(**context_kwargs)
        )

    # ── age: boundaries ──────────────────────────────────────────

    def test_birth_year_boundaries_are_inclusive(self):
        recruitment = self._recruitment()
        RecruitmentAgeCategory.objects.create(
            recruitment=recruitment, title="U15",
            min_birth_year=2008, max_birth_year=2010,
        )

        # Both bounds are themselves eligible years.
        self.assertTrue(self._verdict(recruitment, birth_year=2008).is_eligible)
        self.assertTrue(self._verdict(recruitment, birth_year=2010).is_eligible)
        self.assertTrue(self._verdict(recruitment, birth_year=2009).is_eligible)

        # One year outside either end is not.
        self.assertFalse(self._verdict(recruitment, birth_year=2007).is_eligible)
        self.assertFalse(self._verdict(recruitment, birth_year=2011).is_eligible)

    def test_open_ended_bands_never_exclude_on_the_null_side(self):
        min_only = self._recruitment()
        RecruitmentAgeCategory.objects.create(
            recruitment=min_only, title="U17",
            min_birth_year=2010, max_birth_year=None,
        )
        # "born 2010 or later" — the null max must not close the band.
        self.assertTrue(self._verdict(min_only, birth_year=2010).is_eligible)
        self.assertTrue(self._verdict(min_only, birth_year=2020).is_eligible)
        self.assertFalse(self._verdict(min_only, birth_year=2009).is_eligible)

        max_only = self._recruitment()
        RecruitmentAgeCategory.objects.create(
            recruitment=max_only, title="Veterans",
            min_birth_year=None, max_birth_year=1991,
        )
        # "born 1991 or earlier" — the null min must not close it either.
        self.assertTrue(self._verdict(max_only, birth_year=1991).is_eligible)
        self.assertTrue(self._verdict(max_only, birth_year=1960).is_eligible)
        self.assertFalse(self._verdict(max_only, birth_year=1992).is_eligible)

    def test_bounds_must_be_satisfied_by_the_same_category(self):
        # A min from one group and a max from another is NOT a match — the same
        # rule the birth_year SQL filter enforces by putting both conditions in
        # one .filter() call.
        recruitment = self._recruitment()
        RecruitmentAgeCategory.objects.create(
            recruitment=recruitment, title="U13",
            min_birth_year=2012, max_birth_year=2014,
        )
        RecruitmentAgeCategory.objects.create(
            recruitment=recruitment, title="Seniors",
            min_birth_year=1990, max_birth_year=2000,
        )

        # 2005 clears U13's max and Seniors' min, but satisfies neither row.
        self.assertFalse(self._verdict(recruitment, birth_year=2005).is_eligible)
        self.assertTrue(self._verdict(recruitment, birth_year=2013).is_eligible)

        # The SQL birth_year filter has to agree — one interpretation, not two.
        matched = RecruitmentSelector.build_list_queryset(
            actor=None, birth_year=2005
        ).values_list("id", flat=True)
        self.assertNotIn(recruitment.id, list(matched))

    # ── missing data is never a disqualifier ─────────────────────

    def test_missing_birthdate_is_eligible(self):
        recruitment = self._recruitment()
        RecruitmentAgeCategory.objects.create(
            recruitment=recruitment, title="U15",
            min_birth_year=2008, max_birth_year=2010,
        )

        # Young players are exactly the demographic that leaves DOB blank.
        # Unknown is not ineligible.
        verdict = self._verdict(recruitment, birth_year=None)
        self.assertTrue(verdict.is_eligible)
        self.assertIsNone(verdict.badge)

    def test_missing_gender_is_eligible(self):
        recruitment = self._recruitment(gender=Recruitment.Gender.FEMALE)
        self.assertTrue(self._verdict(recruitment, gender="").is_eligible)

    def test_recruitment_with_no_age_categories_is_eligible(self):
        # An empty list already means "open to all ages".
        recruitment = self._recruitment()
        self.assertTrue(self._verdict(recruitment, birth_year=1975).is_eligible)

    def test_blank_and_all_recruitment_gender_are_eligible(self):
        blank = self._recruitment(gender="")
        self.assertTrue(self._verdict(blank, gender="female").is_eligible)

        every = self._recruitment(gender=Recruitment.Gender.ALL)
        self.assertTrue(self._verdict(every, gender="female").is_eligible)

    # ── badges ───────────────────────────────────────────────────

    def test_age_badge_names_the_groups_it_is_open_to(self):
        recruitment = self._recruitment()
        RecruitmentAgeCategory.objects.create(
            recruitment=recruitment, title="U-17",
            min_birth_year=2010, max_birth_year=2011,
        )

        verdict = self._verdict(recruitment, birth_year=1990)
        self.assertFalse(verdict.is_eligible)
        self.assertEqual(verdict.reasons, ["age"])
        self.assertEqual(verdict.badge, "U-17 only")

    def test_gender_badge_is_informational_not_prohibitive(self):
        recruitment = self._recruitment(gender=Recruitment.Gender.FEMALE)
        verdict = self._verdict(recruitment, gender="male")

        self.assertFalse(verdict.is_eligible)
        self.assertEqual(verdict.badge, "Open to female only")
        # Never phrased as a prohibition — Goatza displays, the venue verifies.
        self.assertNotIn("cannot", verdict.badge.lower())

    def test_deadline_badge_wins_when_several_checks_fail(self):
        recruitment = self._recruitment(
            gender=Recruitment.Gender.FEMALE,
            application_deadline=timezone.now() - timedelta(days=1),
        )
        RecruitmentAgeCategory.objects.create(
            recruitment=recruitment, title="U-17",
            min_birth_year=2010, max_birth_year=2011,
        )

        verdict = self._verdict(recruitment, birth_year=1990, gender="male")
        self.assertEqual(verdict.reasons, ["deadline", "age", "gender"])
        self.assertEqual(verdict.badge, "Applications closed")


class RecruitmentMatchScoreTests(APITestCase):
    """§3 — the additive score, verified against the spec's worked example."""

    def setUp(self):
        self.player = User.objects.create_user(
            email="score_p@example.com", password="pass1234",
            username="score_player",
        )
        accept_current_terms(self.player)
        today = timezone.now().date()
        self.profile = UserProfile.objects.create(
            user=self.player, name="Striker",
            birthdate=today.replace(year=today.year - 16),
            gender="male",
            latitude=TVM_LAT, longitude=TVM_LNG,
        )

        self.academy = Organization.objects.create(
            name="Academy X", username="academyx",
            type=Organization.Type.ACADEMY,
        )
        self.district = Organization.objects.create(
            name="District Board", username="districtboard",
            type=Organization.Type.CLUB,
        )

        self.football = Sport.objects.create(name="Football", icon_name="mdi:soccer")
        self.basketball = Sport.objects.create(
            name="Basketball", icon_name="mdi:basketball"
        )
        self.striker = SportPosition.objects.create(
            sport=self.football, name="Striker"
        )

        UserSport.objects.create(
            user=self.player, sport=self.football, is_primary=True
        )
        UserSportPosition.objects.create(
            user=self.player, sport=self.football,
            position=self.striker, is_primary=True,
        )

        # Follows Academy X — worth +10 on its postings.
        Follow.objects.create(
            follower_user=self.player, following_org=self.academy
        )

        self.actor = Actor(actor_type="user", user=self.player)
        self.now = timezone.now()

    # ── helpers ──────────────────────────────────────────────────

    def _recruitment(self, org=None, sport=None, km=None, **overrides):
        lat, lng = (
            (TVM_LAT + km * DEG_PER_KM, TVM_LNG)
            if km is not None
            else (None, None)
        )
        data = dict(
            organization=org or self.academy,
            sport=sport or self.football,
            status=Recruitment.Status.ACTIVE,
            visibility=Recruitment.Visibility.PUBLIC,
            title="Trial",
            recruitment_type="open_trial",
            latitude=lat,
            longitude=lng,
            published_at=self.now,
        )
        data.update(overrides)
        return Recruitment.objects.create(**data)

    def _score(self, recruitment):
        """Score one row the way the endpoint does — distance from SQL."""
        context = PlayerContextSelector.resolve(self.actor)
        queryset = Recruitment.objects.filter(id=recruitment.id)
        if context.center:
            queryset = RecruitmentSelector.annotate_distance(
                queryset, context.center
            )
        row = queryset.select_related("organization").first()
        return MatchScoreService.score(row, context, self.now)

    def _eligible_group(self, recruitment):
        """An age band the fixture player falls inside."""
        year = self.profile.birthdate.year
        RecruitmentAgeCategory.objects.create(
            recruitment=recruitment, title="U-17",
            min_birth_year=year - 1, max_birth_year=year + 1,
        )

    # ── §3 worked example ────────────────────────────────────────

    def test_worked_example_row_1_scores_96(self):
        # Academy X U-17 striker trial, 8 km away, closes in 5 days, posted
        # yesterday: 40 + 15 + 15 + 8 + 10 + 8.
        recruitment = self._recruitment(
            km=8,
            application_deadline=self.now + timedelta(days=5),
            published_at=self.now - timedelta(days=1),
        )
        RecruitmentPosition.objects.create(
            recruitment=recruitment, position=self.striker
        )
        self._eligible_group(recruitment)

        match = self._score(recruitment)

        self.assertTrue(match.is_eligible)
        self.assertEqual(match.score, 96)
        self.assertEqual(match.sport_match, "primary")
        self.assertTrue(match.position_match)
        self.assertEqual(match.days_to_deadline, 5)

    def test_worked_example_row_2_scores_60(self):
        # District football trial, any position, 40 km, closes in 20 days,
        # posted 5 days ago: 40 + 8 + 8 + 0 + 0 + 4.
        recruitment = self._recruitment(
            org=self.district,
            km=40,
            application_deadline=self.now + timedelta(days=20),
            published_at=self.now - timedelta(days=5),
        )

        match = self._score(recruitment)

        self.assertEqual(match.score, 60)
        # "Any position" is neutral, not a mismatch.
        self.assertIsNone(match.position_match)

    def test_worked_example_row_3_scores_31(self):
        # Basketball scholarship, 5 km, fresh: 0 + 8 + 15 + 0 + 0 + 8.
        recruitment = self._recruitment(
            org=self.district,
            sport=self.basketball,
            km=5,
            recruitment_type="scholarship",
        )

        match = self._score(recruitment)

        self.assertEqual(match.score, 31)
        self.assertEqual(match.sport_match, "none")

    def test_worked_example_row_4_ineligible_sinks_to_about_3(self):
        # Senior (age-ineligible) football trial nearby:
        # (40 + 8 + 15) x 0.05 — bottom of the list, still visible, badged.
        recruitment = self._recruitment(
            org=self.district,
            km=5,
            published_at=self.now - timedelta(days=30),
        )
        RecruitmentAgeCategory.objects.create(
            recruitment=recruitment, title="Seniors",
            min_birth_year=1980, max_birth_year=1995,
        )

        match = self._score(recruitment)

        self.assertEqual(match.raw_score, 63)
        self.assertAlmostEqual(match.score, 3.15, places=2)
        self.assertFalse(match.is_eligible)
        self.assertEqual(match.badge, "Seniors only")

    # ── missing data ─────────────────────────────────────────────

    def test_recruitment_without_coordinates_scores_neutral_five(self):
        # 40 (sport) + 8 (no positions) + 5 (unknown distance) + 8 (fresh).
        recruitment = self._recruitment(km=None, org=self.district)

        match = self._score(recruitment)

        self.assertIsNone(match.distance_km)
        self.assertEqual(match.score, 61)

    def test_player_without_coordinates_scores_neutral_five(self):
        # The other direction: the recruitment has a venue, the player has no
        # location on file. Still +5, never 0.
        self.profile.latitude = None
        self.profile.longitude = None
        self.profile.save(update_fields=["latitude", "longitude"])

        recruitment = self._recruitment(km=5, org=self.district)

        self.assertIsNone(PlayerContextSelector.resolve(self.actor).center)

        match = self._score(recruitment)
        self.assertIsNone(match.distance_km)
        self.assertEqual(match.score, 61)

    def test_player_without_positions_is_neutral_not_a_mismatch(self):
        UserSportPosition.objects.filter(user=self.player).delete()

        recruitment = self._recruitment(km=5, org=self.district)
        RecruitmentPosition.objects.create(
            recruitment=recruitment, position=self.striker
        )

        match = self._score(recruitment)

        self.assertIsNone(match.position_match)
        # 40 + 8 (neutral) + 15 + 8 — the same as an unspecified recruitment.
        self.assertEqual(match.score, 71)

    def test_distance_bands(self):
        for km, points in ((5, 15), (20, 12), (40, 8), (80, 4), (300, 0)):
            with self.subTest(km=km):
                recruitment = self._recruitment(km=km, org=self.district)
                # 40 (sport) + 8 (no positions) + 8 (fresh) + distance band.
                self.assertEqual(self._score(recruitment).score, 56 + points)

    def test_verified_organization_adds_three(self):
        self.district.is_verified = True
        self.district.save(update_fields=["is_verified"])

        recruitment = self._recruitment(km=5, org=self.district)

        # 40 + 8 + 15 + 8 + 3.
        self.assertEqual(self._score(recruitment).score, 74)

    def test_weights_live_in_one_block(self):
        # §8 retunes these from logged outcomes; that is only possible while
        # they are data in one place.
        self.assertEqual(MATCH_WEIGHTS["sport_primary"], 40)
        self.assertEqual(MATCH_WEIGHTS["distance_unknown"], 5)
        self.assertEqual(MATCH_WEIGHTS["ineligible_multiplier"], 0.05)


class RecruitmentDiscoverAPITests(APITestCase):
    """§4 — the /discover payload: sections, dedup, and the degraded actors."""

    DISCOVER_URL = "/recruitments/discover"

    def setUp(self):
        cache.clear()  # the payload is cached per actor for 10 minutes

        self.player = User.objects.create_user(
            email="disc_api@example.com", password="pass1234",
            username="disc_api_player",
        )
        accept_current_terms(self.player)
        today = timezone.now().date()
        self.profile = UserProfile.objects.create(
            user=self.player, name="Player",
            birthdate=today.replace(year=today.year - 16),
            gender="male",
            latitude=TVM_LAT, longitude=TVM_LNG,
        )

        self.owner = User.objects.create_user(
            email="disc_api_o@example.com", password="pass1234",
            username="disc_api_owner",
        )
        accept_current_terms(self.owner)
        self.org = Organization.objects.create(
            name="Coastal FC", username="coastalfc",
            type=Organization.Type.CLUB,
        )
        OrganizationMember.objects.create(
            organization=self.org, user=self.owner,
            role=OrganizationMember.Role.OWNER,
        )

        self.football = Sport.objects.create(name="Football", icon_name="mdi:soccer")
        self.basketball = Sport.objects.create(
            name="Basketball", icon_name="mdi:basketball"
        )
        self.striker = SportPosition.objects.create(
            sport=self.football, name="Striker"
        )
        UserSport.objects.create(
            user=self.player, sport=self.football, is_primary=True
        )
        UserSport.objects.create(
            user=self.player, sport=self.basketball, is_primary=False
        )

        self.now = timezone.now()

    # ── helpers ──────────────────────────────────────────────────

    def _recruitment(self, km=None, **overrides):
        lat, lng = (
            (TVM_LAT + km * DEG_PER_KM, TVM_LNG)
            if km is not None
            else (None, None)
        )
        data = dict(
            organization=self.org,
            sport=self.football,
            status=Recruitment.Status.ACTIVE,
            visibility=Recruitment.Visibility.PUBLIC,
            title="Trial",
            recruitment_type="open_trial",
            latitude=lat,
            longitude=lng,
            published_at=self.now,
        )
        data.update(overrides)
        return Recruitment.objects.create(**data)

    def _discover(self, actor_org=None, **params):
        self.client.force_authenticate(
            user=self.owner if actor_org else self.player
        )
        headers = {}
        if actor_org is not None:
            headers = {
                "HTTP_X_ACTOR_TYPE": "organization",
                "HTTP_X_ACTOR_ID": str(actor_org.id),
            }
        return self.client.get(self.DISCOVER_URL, params, **headers)

    @staticmethod
    def _ids(data, section):
        return [str(item["id"]) for item in data[section]]

    # ── sections + dedup ─────────────────────────────────────────

    def test_sections_are_deduplicated_in_priority_order(self):
        # Two recruitments that each qualify for all four sections: nearby,
        # closing within a week, published today.
        star = self._recruitment(
            km=3, application_deadline=self.now + timedelta(days=2)
        )
        other = self._recruitment(
            km=6, application_deadline=self.now + timedelta(days=3)
        )

        resp = self._discover()
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.data["data"]

        every_id = (
            self._ids(data, "recommended")
            + self._ids(data, "closing_soon")
            + self._ids(data, "near_you")
            + self._ids(data, "new_this_week")
        )
        self.assertEqual(len(every_id), len(set(every_id)))
        self.assertCountEqual(
            self._ids(data, "recommended"), [str(star.id), str(other.id)]
        )
        # Both were consumed by the highest-priority section they qualified for.
        self.assertEqual(self._ids(data, "closing_soon"), [])
        self.assertEqual(self._ids(data, "near_you"), [])
        self.assertEqual(self._ids(data, "new_this_week"), [])

    def test_near_you_holds_what_recommended_did_not_take(self):
        # Ten strong matches saturate "recommended" (score 60 each: primary
        # sport + neutral position + 60 km band + fresh)...
        for index in range(10):
            self._recruitment(km=60, title=f"Filler {index}")
        # ...leaving these two — a weaker secondary-sport pair — to the rails.
        near = self._recruitment(km=20, sport=self.basketball, title="Near")
        far = self._recruitment(km=120, sport=self.basketball, title="Far")

        data = self._discover(max_distance_km=50).data["data"]

        self.assertEqual(len(data["recommended"]), 10)
        self.assertEqual(self._ids(data, "near_you"), [str(near.id)])
        self.assertNotIn(str(far.id), self._ids(data, "near_you"))

    def test_sections_carry_chip_data_not_a_number_the_card_shows(self):
        recruitment = self._recruitment(
            km=8, application_deadline=self.now + timedelta(days=5)
        )
        RecruitmentPosition.objects.create(
            recruitment=recruitment, position=self.striker
        )
        UserSportPosition.objects.create(
            user=self.player, sport=self.football, position=self.striker
        )

        item = self._discover().data["data"]["recommended"][0]

        self.assertEqual(item["sport_match"], "primary")
        self.assertTrue(item["position_match"])
        # The chip names the position that ACTUALLY overlapped, so a posting
        # listing three roles cannot make the card claim the wrong one.
        self.assertEqual(item["matched_positions"], ["Striker"])
        self.assertAlmostEqual(item["distance_km"], 8.0, delta=0.3)
        self.assertEqual(item["days_to_deadline"], 5)
        self.assertTrue(item["is_eligible"])
        self.assertIsNone(item["eligibility_badge"])
        # published_at / application_deadline are on the DISCOVER serializer.
        self.assertIn("published_at", item)
        self.assertIn("application_deadline", item)

    def test_ineligible_rows_rank_last_and_keep_a_badge(self):
        eligible = self._recruitment(km=40, title="Open to me")
        ineligible = self._recruitment(km=1, title="Seniors only")
        RecruitmentAgeCategory.objects.create(
            recruitment=ineligible, title="Seniors",
            min_birth_year=1980, max_birth_year=1995,
        )

        data = self._discover().data["data"]
        recommended = data["recommended"]
        ids = [str(item["id"]) for item in recommended]

        # Visible — never filtered out — but below the eligible row it would
        # otherwise have beaten on distance.
        self.assertIn(str(ineligible.id), ids)
        self.assertEqual(ids[-1], str(ineligible.id))
        self.assertEqual(ids[0], str(eligible.id))

        sunk = recommended[-1]
        self.assertFalse(sunk["is_eligible"])
        self.assertEqual(sunk["eligibility_badge"], "Seniors only")

        # And it is not repeated in the eligible-only rails.
        self.assertNotIn(str(ineligible.id), self._ids(data, "near_you"))

    def test_deadline_passed_rows_are_excluded_from_discover(self):
        closed = self._recruitment(
            km=2, application_deadline=self.now - timedelta(days=1)
        )

        data = self._discover().data["data"]

        for section in (
            "recommended", "closing_soon", "near_you", "new_this_week"
        ):
            self.assertNotIn(str(closed.id), self._ids(data, section))

    # ── degraded actors ──────────────────────────────────────────

    def test_org_actor_gets_a_valid_non_personalized_payload(self):
        self._recruitment(km=5)

        resp = self._discover(actor_org=self.org)

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.data["data"]
        self.assertFalse(data["is_personalized"])
        self.assertIn("sport", data["missing_profile_fields"])
        self.assertEqual(len(data["recommended"]), 1)
        # Nothing personalized to say, so no sport chip.
        self.assertEqual(data["recommended"][0]["sport_match"], "none")

    def test_player_without_sports_gets_a_valid_payload(self):
        UserSport.objects.filter(user=self.player).delete()
        self._recruitment(km=5)

        resp = self._discover()

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.data["data"]
        self.assertFalse(data["is_personalized"])
        self.assertEqual(len(data["recommended"]), 1)

    def test_missing_profile_fields_names_each_gap(self):
        self.profile.birthdate = None
        self.profile.latitude = None
        self.profile.longitude = None
        self.profile.save()

        data = self._discover().data["data"]

        self.assertTrue(data["is_personalized"])  # sport is on file
        self.assertCountEqual(
            data["missing_profile_fields"],
            ["positions", "birthdate", "location"],
        )

    # ── cache ──────────────────────────────────────────────────

    def test_payload_is_cached_per_actor(self):
        self._recruitment(km=5)
        first = self._discover().data["data"]

        # A row published after the first call must not appear until the
        # 10-minute window rolls over (§4 — freshness tolerates it).
        self._recruitment(km=6, title="Newer")
        self.assertEqual(self._discover().data["data"], first)

        cache.clear()
        self.assertEqual(len(self._discover().data["data"]["recommended"]), 2)


class RecruitmentAllTabFilterTests(APITestCase):
    """§4 — the "All" tab: new filters and the ranked default ordering."""

    LIST_URL = "/recruitments/list"

    def setUp(self):
        cache.clear()
        self.player = User.objects.create_user(
            email="all_tab@example.com", password="pass1234",
            username="all_tab_player",
        )
        accept_current_terms(self.player)
        today = timezone.now().date()
        self.profile = UserProfile.objects.create(
            user=self.player, name="Player",
            birthdate=today.replace(year=today.year - 16),
            latitude=TVM_LAT, longitude=TVM_LNG,
        )
        self.owner = User.objects.create_user(
            email="all_tab_o@example.com", password="pass1234",
            username="all_tab_owner",
        )
        accept_current_terms(self.owner)
        self.org = Organization.objects.create(
            name="Harbour FC", username="harbourfc",
            type=Organization.Type.CLUB,
        )
        # The org-scoped list resolves this handle through UsernameRegistry.
        UsernameService.claim(self.org.username, organization=self.org)
        OrganizationMember.objects.create(
            organization=self.org, user=self.owner,
            role=OrganizationMember.Role.OWNER,
        )
        self.football = Sport.objects.create(name="Football", icon_name="mdi:soccer")
        self.striker = SportPosition.objects.create(
            sport=self.football, name="Striker"
        )
        self.keeper = SportPosition.objects.create(
            sport=self.football, name="Goalkeeper"
        )
        UserSport.objects.create(
            user=self.player, sport=self.football, is_primary=True
        )
        self.now = timezone.now()

    def _recruitment(self, km=None, **overrides):
        lat, lng = (
            (TVM_LAT + km * DEG_PER_KM, TVM_LNG)
            if km is not None
            else (None, None)
        )
        data = dict(
            organization=self.org,
            sport=self.football,
            status=Recruitment.Status.ACTIVE,
            visibility=Recruitment.Visibility.PUBLIC,
            title="Trial",
            recruitment_type="open_trial",
            latitude=lat,
            longitude=lng,
            published_at=self.now,
        )
        data.update(overrides)
        return Recruitment.objects.create(**data)

    def _list(self, user=None, org=None, **params):
        self.client.force_authenticate(user=user or self.player)
        headers = {}
        if org is not None:
            headers = {
                "HTTP_X_ACTOR_TYPE": "organization",
                "HTTP_X_ACTOR_ID": str(org.id),
            }
        return self.client.get(self.LIST_URL, params, **headers)

    def _ids(self, resp):
        return [str(r["id"]) for r in resp.data["data"]["results"]]

    # ── ordering ─────────────────────────────────────────────────

    def test_player_gets_match_ordering_not_newest_first(self):
        # Older but a much better match (nearby) vs newer and far away.
        near = self._recruitment(km=2, published_at=self.now - timedelta(days=6))
        far = self._recruitment(km=300, published_at=self.now)

        self.assertEqual(self._ids(self._list()), [str(near.id), str(far.id)])

    def test_org_scoped_list_keeps_published_ordering(self):
        # The org admin and public-org-profile mounts pass ``username`` and must
        # not change behaviour.
        older = self._recruitment(
            km=2, published_at=self.now - timedelta(days=6)
        )
        newer = self._recruitment(km=300, published_at=self.now)

        resp = self._list(username=self.org.username)

        self.assertEqual(self._ids(resp), [str(newer.id), str(older.id)])
        # And the unranked payload keeps the plain list shape.
        self.assertNotIn("match_score", resp.data["data"]["results"][0])

    # ── new filters ──────────────────────────────────────────────

    def test_max_distance_km_filter(self):
        near = self._recruitment(km=20)
        far = self._recruitment(km=120)
        nowhere = self._recruitment(km=None)

        ids = self._ids(self._list(max_distance_km=50))

        self.assertEqual(ids, [str(near.id)])
        self.assertNotIn(str(far.id), ids)
        # A venue with no coordinates cannot answer "within 50 km".
        self.assertNotIn(str(nowhere.id), ids)

    def test_position_id_filter(self):
        striker_trial = self._recruitment()
        RecruitmentPosition.objects.create(
            recruitment=striker_trial, position=self.striker
        )
        keeper_trial = self._recruitment()
        RecruitmentPosition.objects.create(
            recruitment=keeper_trial, position=self.keeper
        )

        ids = self._ids(self._list(position_id=str(self.striker.id)))
        self.assertEqual(ids, [str(striker_trial.id)])

    def test_age_eligible_toggle_filters_only_on_age(self):
        year = self.profile.birthdate.year
        mine = self._recruitment(title="Mine")
        RecruitmentAgeCategory.objects.create(
            recruitment=mine, title="U-17",
            min_birth_year=year - 1, max_birth_year=year + 1,
        )
        seniors = self._recruitment(title="Seniors")
        RecruitmentAgeCategory.objects.create(
            recruitment=seniors, title="Seniors",
            min_birth_year=1980, max_birth_year=1995,
        )
        closed = self._recruitment(
            title="Closed", application_deadline=self.now - timedelta(days=1)
        )

        ids = self._ids(self._list(age_eligible="true"))

        self.assertIn(str(mine.id), ids)
        self.assertNotIn(str(seniors.id), ids)
        # The toggle is about AGE. A closed posting is still theirs to see and
        # keeps its badge — "All" is where deadline-passed rows live.
        self.assertIn(str(closed.id), ids)

    def test_age_eligible_keeps_everything_when_birthdate_is_missing(self):
        self.profile.birthdate = None
        self.profile.save(update_fields=["birthdate"])

        seniors = self._recruitment()
        RecruitmentAgeCategory.objects.create(
            recruitment=seniors, title="Seniors",
            min_birth_year=1980, max_birth_year=1995,
        )

        self.assertIn(
            str(seniors.id), self._ids(self._list(age_eligible="true"))
        )

    def test_rail_deep_link_filters(self):
        # What "Closing soon" / "New this week" mean as a filter, so their
        # "See all" opens the same rule the rail was built from.
        closing = self._recruitment(
            title="Closing", application_deadline=self.now + timedelta(days=3)
        )
        later = self._recruitment(
            title="Later", application_deadline=self.now + timedelta(days=30)
        )
        old = self._recruitment(
            title="Old", published_at=self.now - timedelta(days=30)
        )

        ids = self._ids(self._list(closing_within_days=7))
        self.assertEqual(ids, [str(closing.id)])
        self.assertNotIn(str(later.id), ids)

        ids = self._ids(self._list(published_within_days=7))
        self.assertNotIn(str(old.id), ids)
        self.assertIn(str(closing.id), ids)

        # Junk is ignored, not a 400 — same leniency as every other filter.
        self.assertEqual(
            len(self._ids(self._list(closing_within_days="abc"))), 3
        )

    def test_deadline_passed_rows_stay_in_all_with_a_badge(self):
        closed = self._recruitment(
            application_deadline=self.now - timedelta(days=2)
        )

        results = self._list().data["data"]["results"]

        row = next(r for r in results if str(r["id"]) == str(closed.id))
        self.assertFalse(row["is_eligible"])
        self.assertEqual(row["eligibility_badge"], "Applications closed")


class RecruitmentApplyFlowIsolationTests(APITestCase):
    """
    The guard against this feature leaking into the apply flow.

    Eligibility is display + ranking. ``is_accepting_applications`` is the hard
    gate, and it has to behave exactly as it did before ranking existed.
    """

    def setUp(self):
        cache.clear()
        self.player = User.objects.create_user(
            email="iso_p@example.com", password="pass1234",
            username="iso_player",
        )
        accept_current_terms(self.player)
        today = timezone.now().date()
        UserProfile.objects.create(
            user=self.player, name="Player",
            birthdate=today.replace(year=today.year - 16),
            gender="male",
        )
        self.org = Organization.objects.create(
            name="Gate FC", username="gatefc", type=Organization.Type.CLUB,
        )
        self.sport = Sport.objects.create(name="Football", icon_name="mdi:soccer")

    def test_is_accepting_applications_ignores_player_fit(self):
        recruitment = Recruitment.objects.create(
            organization=self.org, sport=self.sport,
            status=Recruitment.Status.ACTIVE,
            visibility=Recruitment.Visibility.PUBLIC,
            title="Seniors", recruitment_type="open_trial",
            gender=Recruitment.Gender.FEMALE,
            published_at=timezone.now(),
        )
        RecruitmentAgeCategory.objects.create(
            recruitment=recruitment, title="Seniors",
            min_birth_year=1980, max_birth_year=1995,
        )

        # Age AND gender both fail for this player...
        context = PlayerContextSelector.resolve(
            Actor(actor_type="user", user=self.player)
        )
        self.assertFalse(
            eligibility_service.evaluate(recruitment, context).is_eligible
        )

        # ...and neither touches the hard gate or the Apply button.
        self.assertTrue(recruitment.is_accepting_applications)

        self.client.force_authenticate(user=self.player)
        resp = self.client.get(f"/recruitments/{recruitment.id}/details")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertTrue(resp.data["data"]["is_accepting_applications"])
        self.assertTrue(resp.data["data"]["can_apply"])

    def test_is_accepting_applications_still_gates_the_hard_states(self):
        # The unchanged behaviour, restated here so a regression in ranking
        # cannot quietly widen it.
        recruitment = Recruitment.objects.create(
            organization=self.org, sport=self.sport,
            status=Recruitment.Status.ACTIVE,
            title="Gate", recruitment_type="open_trial",
        )
        self.assertTrue(recruitment.is_accepting_applications)

        recruitment.status = Recruitment.Status.CLOSED
        self.assertFalse(recruitment.is_accepting_applications)

        recruitment.status = Recruitment.Status.ACTIVE
        recruitment.max_applications = 1
        recruitment.applications_count = 1
        self.assertFalse(recruitment.is_accepting_applications)

        recruitment.applications_count = 0
        recruitment.application_deadline = timezone.now() - timedelta(days=1)
        self.assertFalse(recruitment.is_accepting_applications)


# =====================================================================
# SAVED RECRUITMENTS — the shortlist, per ACTOR, private to the saver
# =====================================================================

SAVED_LIST_URL = "/recruitments/saved/list"


class SavedRecruitmentTests(APITestCase):
    """
    A save belongs to the actor that made it: a person and an org they run
    keep completely separate shortlists of the same recruitment.

    The one deliberate difference from saved posts is what the list KEEPS —
    a closed trial stays, because a shortlist is where a player notices the
    deadline passed.
    """

    def setUp(self):
        cache.clear()  # the discover payload is cached per actor

        self.me = self._user("saver", "Saver")
        self.other = self._user("stranger", "Stranger")

        self.org = Organization.objects.create(
            name="Shortlist FC", username="shortlistfc",
            type=Organization.Type.CLUB,
        )
        OrganizationMember.objects.create(
            organization=self.org, user=self.me,
            role=OrganizationMember.Role.OWNER,
        )
        # The org-scoped list resolves ?username= through UsernameRegistry.
        UsernameService.claim(self.org.username, organization=self.org)

        self.sport = Sport.objects.create(name="Football", icon_name="mdi:soccer")

        self.client.force_authenticate(user=self.me)

    # ── factories ────────────────────────────────────────────────

    def _user(self, username, name):
        user = User.objects.create_user(
            email=f"{username}@example.com", password="pass1234",
            username=username,
        )
        accept_current_terms(user)
        UserProfile.objects.create(user=user, name=name)
        return user

    def _recruitment(self, title="Trial", **overrides):
        data = dict(
            organization=self.org,
            sport=self.sport,
            title=title,
            recruitment_type="open_trial",
            status=Recruitment.Status.ACTIVE,
            visibility=Recruitment.Visibility.PUBLIC,
            published_at=timezone.now(),
        )
        data.update(overrides)
        return Recruitment.objects.create(**data)

    def _org_headers(self):
        return {
            "HTTP_X_ACTOR_TYPE": "organization",
            "HTTP_X_ACTOR_ID": str(self.org.id),
        }

    def _toggle(self, recruitment_id, as_org=False):
        headers = self._org_headers() if as_org else {}
        return self.client.post(
            f"/recruitments/{recruitment_id}/save", **headers
        )

    def _saved_list(self, as_org=False, **params):
        headers = self._org_headers() if as_org else {}
        return self.client.get(SAVED_LIST_URL, params, **headers)

    def _list_row(self, recruitment_id, **params):
        """The card as /recruitments/list serializes it, for the annotation."""
        resp = self.client.get("/recruitments/list", params)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        rows = [
            row for row in resp.data["data"]["results"]
            if row["id"] == str(recruitment_id)
        ]
        self.assertEqual(len(rows), 1, resp.data["data"]["results"])
        return rows[0]

    def _detail(self, recruitment_id):
        resp = self.client.get(f"/recruitments/{recruitment_id}/details")
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        return resp.data["data"]

    # ── toggle as a user ─────────────────────────────────────────

    def test_toggle_as_user_saves_then_unsaves(self):
        recruitment = self._recruitment()

        on = self._toggle(recruitment.id)
        self.assertEqual(on.status_code, status.HTTP_200_OK, on.data)
        self.assertTrue(on.data["data"]["is_saved"])
        self.assertEqual(
            on.data["data"]["recruitment_id"], str(recruitment.id)
        )

        row = SavedRecruitment.objects.get(recruitment=recruitment)
        self.assertEqual(row.user_id, self.me.id)
        self.assertIsNone(row.org_id)

        off = self._toggle(recruitment.id)
        self.assertEqual(off.status_code, status.HTTP_200_OK, off.data)
        self.assertFalse(off.data["data"]["is_saved"])
        self.assertFalse(
            SavedRecruitment.objects.filter(recruitment=recruitment).exists()
        )

    # ── the two actors are independent ───────────────────────────

    def test_org_save_is_a_separate_row_and_list(self):
        recruitment = self._recruitment()

        self._toggle(recruitment.id)                 # as me
        self._toggle(recruitment.id, as_org=True)    # as the org

        self.assertEqual(
            SavedRecruitment.objects.filter(recruitment=recruitment).count(), 2
        )
        self.assertTrue(
            SavedRecruitment.objects.filter(
                recruitment=recruitment, user=self.me, org__isnull=True
            ).exists()
        )
        self.assertTrue(
            SavedRecruitment.objects.filter(
                recruitment=recruitment, org=self.org, user__isnull=True
            ).exists()
        )

        # Unsaving as the org leaves the person's save untouched.
        self._toggle(recruitment.id, as_org=True)
        self.assertTrue(
            SavedRecruitment.objects.filter(
                recruitment=recruitment, user=self.me
            ).exists()
        )
        self.assertFalse(
            SavedRecruitment.objects.filter(
                recruitment=recruitment, org=self.org
            ).exists()
        )

    def test_saved_list_is_actor_scoped(self):
        mine = self._recruitment("Mine")
        theirs = self._recruitment("The club's")

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

    # ── constraints ──────────────────────────────────────────────

    def test_second_save_by_the_same_actor_is_rejected(self):
        recruitment = self._recruitment()
        SavedRecruitment.objects.create(recruitment=recruitment, user=self.me)

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                SavedRecruitment.objects.create(
                    recruitment=recruitment, user=self.me
                )

    def test_row_with_both_user_and_org_is_rejected(self):
        recruitment = self._recruitment()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                SavedRecruitment.objects.create(
                    recruitment=recruitment, user=self.me, org=self.org
                )

    def test_row_with_neither_user_nor_org_is_rejected(self):
        recruitment = self._recruitment()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                SavedRecruitment.objects.create(recruitment=recruitment)

    # ── what cannot be saved ─────────────────────────────────────

    def test_saving_a_deleted_recruitment_returns_404(self):
        recruitment = self._recruitment(is_deleted=True)

        resp = self._toggle(recruitment.id)
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND, resp.data)
        self.assertFalse(
            SavedRecruitment.objects.filter(recruitment=recruitment).exists()
        )

    def test_saving_a_hidden_recruitment_returns_404(self):
        # A draft and a private posting are both invisible to a stranger, and
        # saving must not become the way to find out they exist.
        draft = self._recruitment("Draft", status=Recruitment.Status.DRAFT)
        private = self._recruitment(
            "Private", visibility=Recruitment.Visibility.PRIVATE
        )
        followers_only = self._recruitment(
            "Followers", visibility=Recruitment.Visibility.FOLLOWERS_ONLY
        )

        self.client.force_authenticate(user=self.other)

        for recruitment in (draft, private, followers_only):
            resp = self._toggle(recruitment.id)
            self.assertEqual(
                resp.status_code, status.HTTP_404_NOT_FOUND, resp.data
            )

        self.assertEqual(SavedRecruitment.objects.count(), 0)

    def test_saving_an_unknown_recruitment_returns_404(self):
        resp = self._toggle(uuid.uuid4())
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND, resp.data)

    # ── the list ─────────────────────────────────────────────────

    def test_saved_list_is_newest_saved_first(self):
        first = self._recruitment("First")
        second = self._recruitment("Second")
        third = self._recruitment("Third")

        # Saved out of creation order — the LIST order follows the saves.
        self._toggle(second.id)
        self._toggle(third.id)
        self._toggle(first.id)

        resp = self._saved_list()
        self.assertEqual(
            [r["id"] for r in resp.data["data"]["results"]],
            [str(first.id), str(third.id), str(second.id)],
        )

    def test_saved_list_paginates_on_limit_and_offset(self):
        recruitments = [self._recruitment(f"Trial {i}") for i in range(3)]
        for recruitment in recruitments:
            self._toggle(recruitment.id)

        newest_first = [str(r.id) for r in reversed(recruitments)]

        page_one = self._saved_list(limit=2)
        self.assertEqual(page_one.data["data"]["count"], 3)
        self.assertEqual(page_one.data["data"]["limit"], 2)
        self.assertEqual(
            [r["id"] for r in page_one.data["data"]["results"]],
            newest_first[:2],
        )

        page_two = self._saved_list(limit=2, offset=2)
        self.assertEqual(page_two.data["data"]["count"], 3)
        self.assertEqual(page_two.data["data"]["offset"], 2)
        self.assertEqual(
            [r["id"] for r in page_two.data["data"]["results"]],
            newest_first[2:],
        )

    def test_saved_list_carries_the_card_payload(self):
        recruitment = self._recruitment(city="Kozhikode")
        self._toggle(recruitment.id)

        row = self._saved_list().data["data"]["results"][0]
        # The same card the list/discover endpoints ship, so the frontend
        # renders it with RecruitmentCard unchanged...
        self.assertEqual(row["title"], recruitment.title)
        self.assertEqual(row["city"], "Kozhikode")
        self.assertEqual(row["organization"]["username"], "shortlistfc")
        # ...plus the timestamp only this list knows...
        self.assertIsNotNone(row["saved_at"])
        # ...and the flag that keeps THIS list's own bookmark filled.
        self.assertTrue(row["is_saved"])

    def test_unsaving_drops_the_recruitment_from_the_list(self):
        recruitment = self._recruitment()
        self._toggle(recruitment.id)
        self._toggle(recruitment.id)

        resp = self._saved_list()
        self.assertEqual(resp.data["data"]["results"], [])
        self.assertEqual(resp.data["data"]["count"], 0)

    def test_deleted_recruitment_drops_out_of_the_list(self):
        kept = self._recruitment("Kept")
        removed = self._recruitment("Withdrawn")
        self._toggle(kept.id)
        self._toggle(removed.id)

        removed.is_deleted = True
        removed.save(update_fields=["is_deleted"])

        resp = self._saved_list()
        self.assertEqual(
            [r["id"] for r in resp.data["data"]["results"]], [str(kept.id)]
        )
        self.assertEqual(resp.data["data"]["count"], 1)
        # The save row itself survives — nothing rewrites history when an org
        # withdraws a posting; the list just stops showing it.
        self.assertTrue(
            SavedRecruitment.objects.filter(recruitment=removed).exists()
        )

    def test_closed_recruitment_stays_in_the_list_with_its_status(self):
        recruitment = self._recruitment("Closing")
        self._toggle(recruitment.id)

        recruitment.status = Recruitment.Status.CLOSED
        recruitment.save(update_fields=["status"])

        row = self._saved_list().data["data"]["results"][0]
        self.assertEqual(row["id"], str(recruitment.id))
        # A shortlist is exactly where someone notices a deadline passed, so
        # the card stays and wears the badge.
        self.assertEqual(row["status"], Recruitment.Status.CLOSED)

    def test_recruitment_the_saver_can_no_longer_see_drops_out(self):
        recruitment = self._recruitment("Went private")
        self.client.force_authenticate(user=self.other)
        self._toggle(recruitment.id)
        self.assertEqual(len(self._saved_list().data["data"]["results"]), 1)

        recruitment.visibility = Recruitment.Visibility.PRIVATE
        recruitment.save(update_fields=["visibility"])

        resp = self._saved_list()
        self.assertEqual(resp.data["data"]["results"], [])

    def test_followers_only_save_survives_for_a_follower(self):
        recruitment = self._recruitment(
            "Members only", visibility=Recruitment.Visibility.FOLLOWERS_ONLY
        )
        Follow.objects.create(follower_user=self.other, following_org=self.org)

        self.client.force_authenticate(user=self.other)
        self._toggle(recruitment.id)

        resp = self._saved_list()
        self.assertEqual(
            [r["id"] for r in resp.data["data"]["results"]], [str(recruitment.id)]
        )

    # ── is_saved on the existing payloads ────────────────────────

    def test_is_saved_on_the_ranked_list_for_saver_and_non_saver(self):
        recruitment = self._recruitment()

        self.assertFalse(self._list_row(recruitment.id)["is_saved"])

        self._toggle(recruitment.id)
        self.assertTrue(self._list_row(recruitment.id)["is_saved"])

        self.client.force_authenticate(user=self.other)
        self.assertFalse(self._list_row(recruitment.id)["is_saved"])

    def test_is_saved_on_the_org_scoped_list(self):
        # The username-scoped mount takes the PLAIN list serializer, not the
        # ranked one — a second code path, and the bookmark has to survive it.
        recruitment = self._recruitment()
        self._toggle(recruitment.id)

        row = self._list_row(recruitment.id, username="shortlistfc")
        self.assertTrue(row["is_saved"])

    def test_is_saved_on_the_detail_for_saver_and_non_saver(self):
        recruitment = self._recruitment()

        self.assertFalse(self._detail(recruitment.id)["is_saved"])

        self._toggle(recruitment.id)
        self.assertTrue(self._detail(recruitment.id)["is_saved"])

        self.client.force_authenticate(user=self.other)
        self.assertFalse(self._detail(recruitment.id)["is_saved"])

    def test_discover_reflects_a_save_made_after_the_payload_was_cached(self):
        recruitment = self._recruitment()

        first = self.client.get("/recruitments/discover")
        self.assertEqual(first.status_code, status.HTTP_200_OK, first.data)
        rows = first.data["data"]["recommended"]
        self.assertEqual([r["id"] for r in rows], [str(recruitment.id)])
        self.assertFalse(rows[0]["is_saved"])

        self._toggle(recruitment.id)

        # Served from the 10-minute cache, but the bookmark is the viewer's own
        # action and is re-read rather than remembered.
        second = self.client.get("/recruitments/discover")
        self.assertTrue(second.data["data"]["recommended"][0]["is_saved"])

    # ── saves_count (owner-only) ─────────────────────────────────

    def _detail_as_org(self, recruitment_id):
        """The detail payload the OWNING org sees (owner serializer)."""
        resp = self.client.get(
            f"/recruitments/{recruitment_id}/details", **self._org_headers()
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        return resp.data["data"]

    def test_owner_detail_exposes_saves_count(self):
        recruitment = self._recruitment()

        # Two different actors shortlist it: the user, and the org itself. Both
        # are real rows, so the aggregate is 2 — the count is of SAVES, not of
        # people, which is the same rule the dual-actor shortlist is built on.
        SavedRecruitment.objects.create(recruitment=recruitment, user=self.other)
        SavedRecruitment.objects.create(recruitment=recruitment, org=self.org)

        self.assertEqual(self._detail_as_org(recruitment.id)["saves_count"], 2)

    def test_owner_detail_saves_count_is_zero_not_missing(self):
        # A posting nobody saved must still carry the key, so the stats tile
        # renders a 0 rather than an empty cell.
        recruitment = self._recruitment()

        self.assertEqual(self._detail_as_org(recruitment.id)["saves_count"], 0)

    def test_non_owner_detail_has_no_saves_count_key(self):
        recruitment = self._recruitment()
        SavedRecruitment.objects.create(recruitment=recruitment, user=self.other)

        # A plain viewer gets the PUBLIC serializer, which never declares the
        # field — asserted as an absent key, not a falsy value, because "0" and
        # "not yours to see" must not be the same answer on the wire.
        self.client.force_authenticate(user=self.other)
        self.assertNotIn("saves_count", self._detail(recruitment.id))

    def test_other_org_detail_has_no_saves_count_key(self):
        # Acting as a DIFFERENT org is still not the owner.
        recruitment = self._recruitment()
        rival = Organization.objects.create(
            name="Rival FC", username="rivalfc", type=Organization.Type.CLUB,
        )
        OrganizationMember.objects.create(
            organization=rival, user=self.other,
            role=OrganizationMember.Role.OWNER,
        )

        self.client.force_authenticate(user=self.other)
        resp = self.client.get(
            f"/recruitments/{recruitment.id}/details",
            HTTP_X_ACTOR_TYPE="organization",
            HTTP_X_ACTOR_ID=str(rival.id),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertNotIn("saves_count", resp.data["data"])


class PublicRecruitmentDetailTests(APITestCase):
    """
    GET /public/recruitments/<id> — the shareable link.

    Two things are pinned here, and both fail silently if they break: the
    endpoint answers the SAME 404 for every posting a stranger may not see, and
    it serializes with the PUBLIC serializer for every caller INCLUDING the
    posting org. The second is the one an ordinary owner-check refactor would
    undo without a single test going red anywhere else, because the
    authenticated twin is supposed to switch serializers on exactly that
    condition.
    """

    def setUp(self):
        # The view-count latch is a cache key, and cache.add is a no-op the
        # second time inside the window.
        cache.clear()

        self.owner = self._user("clubowner", "Club Owner")
        self.player = self._user("publicplayer", "Player One")
        self.follower = self._user("publicfollower", "Follower")

        self.org = Organization.objects.create(
            name="Public FC", username="publicfc",
            type=Organization.Type.CLUB,
        )
        OrganizationMember.objects.create(
            organization=self.org, user=self.owner,
            role=OrganizationMember.Role.OWNER,
        )

        self.sport = Sport.objects.create(name="Football", icon_name="mdi:soccer")
        self.recruitment = self._recruitment()

    # ── factories ────────────────────────────────────────────────

    def _user(self, username, name):
        user = User.objects.create_user(
            email=f"{username}@example.com", password="pass1234",
            username=username,
        )
        accept_current_terms(user)
        UserProfile.objects.create(user=user, name=name)
        return user

    def _recruitment(self, **overrides):
        data = dict(
            organization=self.org,
            sport=self.sport,
            title="U17 Open Trials",
            recruitment_type="open_trial",
            status=Recruitment.Status.ACTIVE,
            visibility=Recruitment.Visibility.PUBLIC,
            published_at=timezone.now(),
        )
        data.update(overrides)
        return Recruitment.objects.create(**data)

    def _public(self, recruitment_id=None, **extra):
        return self.client.get(
            f"/public/recruitments/{recruitment_id or self.recruitment.id}",
            **extra,
        )

    def _org_headers(self):
        return {
            "HTTP_X_ACTOR_TYPE": "organization",
            "HTTP_X_ACTOR_ID": str(self.org.id),
        }

    # ── who can read it ──────────────────────────────────────────

    def test_anonymous_reads_an_active_public_recruitment(self):
        resp = self._public()

        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertEqual(resp.data["data"]["id"], str(self.recruitment.id))
        self.assertEqual(resp.data["data"]["title"], "U17 Open Trials")

    def test_anonymous_gets_404_for_a_draft(self):
        draft = self._recruitment(
            title="Not live yet", status=Recruitment.Status.DRAFT
        )
        self.assertEqual(
            self._public(draft.id).status_code, status.HTTP_404_NOT_FOUND
        )

    def test_anonymous_gets_404_for_followers_only(self):
        private = self._recruitment(
            title="Members only",
            visibility=Recruitment.Visibility.FOLLOWERS_ONLY,
        )
        self.assertEqual(
            self._public(private.id).status_code, status.HTTP_404_NOT_FOUND
        )

    def test_a_follower_reads_a_followers_only_posting(self):
        # ActorMixin still runs on the public surface, so a signed-in caller is
        # resolved normally and keeps the content they are entitled to.
        private = self._recruitment(
            title="Members only",
            visibility=Recruitment.Visibility.FOLLOWERS_ONLY,
        )
        Follow.objects.create(
            follower_user=self.follower, following_org=self.org
        )

        self.client.force_authenticate(user=self.follower)
        resp = self._public(private.id)

        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertEqual(resp.data["data"]["id"], str(private.id))

    def test_unknown_id_404s_with_the_same_body_as_the_authed_detail(self):
        missing = uuid.uuid4()

        anon = self._public(missing)

        self.client.force_authenticate(user=self.player)
        authed = self.client.get(f"/recruitments/{missing}/details")

        self.assertEqual(anon.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(authed.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(anon.data["message"], authed.data["message"])
        self.assertEqual(anon.data["success"], authed.data["success"])

    # ── what the payload may carry ───────────────────────────────

    OWNER_ONLY_FIELDS = (
        "views_count", "saves_count", "status", "max_applications",
        "confirmed_count", "selected_count", "published_at", "updated_at",
    )

    def test_anonymous_payload_has_no_owner_only_fields(self):
        resp = self._public()

        for field in self.OWNER_ONLY_FIELDS:
            self.assertNotIn(field, resp.data["data"], field)

    def test_the_owner_org_also_gets_the_public_payload_here(self):
        """
        The one place the owner deliberately does NOT get the owner serializer.

        This URL is anonymous, cacheable and shareable, and the numbers on the
        owner payload are exactly the ones that must never appear on it. An org
        admin who wants them opens the authenticated detail — which the second
        half of this test proves still hands them over.
        """
        self.client.force_authenticate(user=self.owner)

        public = self._public(**self._org_headers())
        self.assertEqual(public.status_code, status.HTTP_200_OK, public.data)
        for field in self.OWNER_ONLY_FIELDS:
            self.assertNotIn(field, public.data["data"], field)

        authed = self.client.get(
            f"/recruitments/{self.recruitment.id}/details",
            **self._org_headers(),
        )
        self.assertEqual(authed.status_code, status.HTTP_200_OK, authed.data)
        self.assertIn("views_count", authed.data["data"])
        self.assertIn("saves_count", authed.data["data"])


class RecruitmentViewCountTests(APITestCase):
    """
    ``views_count`` — the only number on a recruitment nobody but the posting
    org ever sees, and until now the only one that never moved.

    Every rule below is one an org would read as a lie if it broke: its own
    opens counting as interest, one player's refresh loop counting as five, or
    a cache outage turning the detail page into a 500.
    """

    def setUp(self):
        cache.clear()

        self.owner = self._user("countowner", "Count Owner")
        self.player = self._user("countplayer", "Count Player")
        self.other = self._user("countother", "Count Other")

        self.org = Organization.objects.create(
            name="Counted FC", username="countedfc",
            type=Organization.Type.CLUB,
        )
        OrganizationMember.objects.create(
            organization=self.org, user=self.owner,
            role=OrganizationMember.Role.OWNER,
        )

        self.sport = Sport.objects.create(name="Football", icon_name="mdi:soccer")
        self.recruitment = Recruitment.objects.create(
            organization=self.org,
            sport=self.sport,
            title="Counted Trial",
            recruitment_type="open_trial",
            status=Recruitment.Status.ACTIVE,
            visibility=Recruitment.Visibility.PUBLIC,
            published_at=timezone.now(),
        )

    def _user(self, username, name):
        user = User.objects.create_user(
            email=f"{username}@example.com", password="pass1234",
            username=username,
        )
        accept_current_terms(user)
        UserProfile.objects.create(user=user, name=name)
        return user

    def _org_headers(self):
        return {
            "HTTP_X_ACTOR_TYPE": "organization",
            "HTTP_X_ACTOR_ID": str(self.org.id),
        }

    def _authed_detail(self, **extra):
        return self.client.get(
            f"/recruitments/{self.recruitment.id}/details", **extra
        )

    def _public_detail(self, **extra):
        return self.client.get(
            f"/public/recruitments/{self.recruitment.id}", **extra
        )

    def _count(self):
        self.recruitment.refresh_from_db(fields=["views_count"])
        return self.recruitment.views_count

    # ── the authenticated detail ─────────────────────────────────

    def test_a_non_owner_read_counts(self):
        self.client.force_authenticate(user=self.player)

        self.assertEqual(self._authed_detail().status_code, status.HTTP_200_OK)
        self.assertEqual(self._count(), 1)

    def test_the_owner_org_never_counts_its_own_views(self):
        self.client.force_authenticate(user=self.owner)

        for _ in range(3):
            self.assertEqual(
                self._authed_detail(**self._org_headers()).status_code,
                status.HTTP_200_OK,
            )

        self.assertEqual(self._count(), 0)

    def test_one_viewer_counts_once_within_the_window(self):
        self.client.force_authenticate(user=self.player)

        for _ in range(4):
            self._authed_detail()

        self.assertEqual(self._count(), 1)

    def test_two_viewers_count_twice(self):
        self.client.force_authenticate(user=self.player)
        self._authed_detail()

        self.client.force_authenticate(user=self.other)
        self._authed_detail()

        self.assertEqual(self._count(), 2)

    def test_the_owner_reads_back_the_players_view(self):
        """The number the org actually sees on its own detail page."""
        self.client.force_authenticate(user=self.player)
        self._authed_detail()

        self.client.force_authenticate(user=self.owner)
        resp = self._authed_detail(**self._org_headers())

        self.assertEqual(resp.data["data"]["views_count"], 1)

    # ── the public detail ────────────────────────────────────────

    def test_an_anonymous_public_read_counts_and_dedupes_by_ip(self):
        for _ in range(3):
            self.assertEqual(
                self._public_detail(REMOTE_ADDR="203.0.113.7").status_code,
                status.HTTP_200_OK,
            )

        self.assertEqual(self._count(), 1)

        self._public_detail(REMOTE_ADDR="203.0.113.8")
        self.assertEqual(self._count(), 2)

    def test_a_viewer_counts_once_across_both_detail_routes(self):
        """
        The latch keys on the ACTOR, not the route. Somebody who opens a shared
        link while signed in and then taps through to the in-app page is one
        interested person, and an org reading "2" there would be reading a
        count of page loads dressed up as a count of people.
        """
        self.client.force_authenticate(user=self.player)

        self._public_detail()
        self._authed_detail()

        self.assertEqual(self._count(), 1)

    def test_a_404_is_not_a_view(self):
        draft = Recruitment.objects.create(
            organization=self.org,
            sport=self.sport,
            title="Draft",
            recruitment_type="open_trial",
            status=Recruitment.Status.DRAFT,
            visibility=Recruitment.Visibility.PUBLIC,
        )

        resp = self.client.get(f"/public/recruitments/{draft.id}")

        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)
        draft.refresh_from_db(fields=["views_count"])
        self.assertEqual(draft.views_count, 0)

    # ── it must never break the read ─────────────────────────────

    def test_a_cache_outage_does_not_break_the_detail_response(self):
        self.client.force_authenticate(user=self.player)

        with patch(
            "apps.recruitments.services.recruitment_view_service.cache_add",
            side_effect=RuntimeError("redis is down"),
        ):
            resp = self._authed_detail()

        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertEqual(self._count(), 0)

    def test_a_database_hiccup_does_not_break_the_detail_response(self):
        self.client.force_authenticate(user=self.player)

        with patch.object(
            Recruitment.objects, "filter",
            side_effect=RuntimeError("connection reset"),
        ):
            resp = self._authed_detail()

        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)

    # ── and it stays owner-only everywhere else ──────────────────

    def test_views_count_is_on_no_non_owner_serializer(self):
        UsernameService.claim(self.org.username, organization=self.org)
        self.client.force_authenticate(user=self.player)

        detail = self._authed_detail()
        self.assertNotIn("views_count", detail.data["data"])

        public = self._public_detail()
        self.assertNotIn("views_count", public.data["data"])

        listing = self.client.get(
            "/recruitments/list", {"username": self.org.username}
        )
        self.assertEqual(listing.status_code, status.HTTP_200_OK, listing.data)
        rows = listing.data["data"]["results"]
        self.assertTrue(rows)
        for row in rows:
            self.assertNotIn("views_count", row)

        discover = self.client.get("/recruitments/discover")
        self.assertEqual(discover.status_code, status.HTTP_200_OK, discover.data)
        for section in SECTION_ORDER:
            for row in discover.data["data"].get(section, []):
                self.assertNotIn("views_count", row)


# =====================================================================
# TRIAL OVER — a trial vanishes from player-facing lists once its day ends
# =====================================================================

from datetime import datetime, time as _dt_time
from zoneinfo import ZoneInfo

from apps.organization.models import OrganizationProfile
from apps.recruitments import trial_window

IST = ZoneInfo("Asia/Kolkata")


class TrialOverTests(APITestCase):
    """
    The trial DAY is the unit, in RECRUITMENT_TIMEZONE (IST here), never the
    instant: a 09:00 trial is still "today" at 20:00 and over at 00:00 the
    next morning. Every clock in these tests is pinned — ``timezone.now`` is
    patched for the request-level checks, and the helpers take ``now``.

    What each surface does with an ended trial:

      hides it   — All tab (ranked + plain), search, another org's profile
                   tab, discover, the logged-out org bundle
      keeps it   — the owner's own list, the shortlist, My applications, and
                   a direct link to the detail — all of them with
                   ``is_trial_over: true``
      refuses    — applying, with "This trial has ended."
    """

    LIST_URL = "/recruitments/list"
    DISCOVER_URL = "/recruitments/discover"
    MY_APPS_URL = "/recruitments/applications/my"

    # Trial day: 15 Sep 2026 IST. The two clocks the tests care about.
    TRIAL_DAY = datetime(2026, 9, 15, tzinfo=IST)
    MORNING = datetime(2026, 9, 15, 8, 0, tzinfo=IST)      # same day, 08:00
    EVENING = datetime(2026, 9, 15, 20, 0, tzinfo=IST)     # same day, 20:00
    NEXT_MORNING = datetime(2026, 9, 16, 0, 30, tzinfo=IST)  # after midnight

    def setUp(self):
        cache.clear()  # discover payload + username lookups + public bundle

        self.player = self._user("trial_player", "Player")
        self.owner = self._user("trial_owner", "Owner")
        self.rival = self._user("trial_rival", "Rival")

        self.org = Organization.objects.create(
            name="Sunrise FC", username="sunrisefc",
            type=Organization.Type.CLUB,
        )
        OrganizationProfile.objects.create(organization=self.org)
        OrganizationMember.objects.create(
            organization=self.org, user=self.owner,
            role=OrganizationMember.Role.OWNER,
        )
        UsernameService.claim(self.org.username, organization=self.org)

        self.other_org = Organization.objects.create(
            name="Rival FC", username="rivalfc",
            type=Organization.Type.CLUB,
        )
        OrganizationMember.objects.create(
            organization=self.other_org, user=self.rival,
            role=OrganizationMember.Role.OWNER,
        )

        self.sport = Sport.objects.create(name="Football", icon_name="mdi:soccer")

    # ── factories ────────────────────────────────────────────────

    def _user(self, username, name):
        user = User.objects.create_user(
            email=f"{username}@example.com", password="pass1234",
            username=username,
        )
        accept_current_terms(user)
        UserProfile.objects.create(user=user, name=name)
        return user

    def _recruitment(self, title="Trial", org=None, **overrides):
        data = dict(
            organization=org or self.org,
            sport=self.sport,
            title=title,
            recruitment_type="open_trial",
            status=Recruitment.Status.ACTIVE,
            visibility=Recruitment.Visibility.PUBLIC,
            published_at=self.TRIAL_DAY,
        )
        data.update(overrides)
        recruitment = Recruitment.objects.create(**data)
        self._give_it_a_session(recruitment)
        return recruitment

    def _give_it_a_session(self, recruitment):
        """
        A trial's dates now live in TrialSession, and event_date /
        trial_end_date are DERIVED from them. Give every fixture the one
        session its event_date stands for — exactly the shape
        ``backfill_trial_sessions`` gives every pre-existing row, so these
        tests keep asking the same question of the new model.
        """
        from apps.recruitments.models import TrialSession
        from apps.recruitments.services.recruitment_service import (
            RecruitmentService,
        )

        if (
            recruitment.recruitment_type != Recruitment.Type.OPEN_TRIAL
            or recruitment.event_date is None
        ):
            return

        local = recruitment.event_date.astimezone(IST)
        start_time = local.time()
        TrialSession.objects.create(
            recruitment=recruitment,
            date=local.date(),
            start_time=(
                None
                if start_time in (_dt_time(23, 59), _dt_time(0, 0))
                else start_time
            ),
        )
        RecruitmentService._sync_trial_window(recruitment)

    def _date_only_trial(self, **overrides):
        """What the frontend stores for a trial with no time: 23:59 local."""
        return self._recruitment(
            "Date-only",
            event_date=datetime(2026, 9, 15, 23, 59, tzinfo=IST),
            **overrides,
        )

    def _timed_trial(self, title="Timed", **overrides):
        return self._recruitment(
            title, event_date=datetime(2026, 9, 15, 9, 0, tzinfo=IST),
            **overrides,
        )

    def _at(self, now):
        """Pin every clock a request reads to ``now``."""
        return patch("django.utils.timezone.now", return_value=now)

    def _org_headers(self, org):
        return {
            "HTTP_X_ACTOR_TYPE": "organization",
            "HTTP_X_ACTOR_ID": str(org.id),
        }

    def _ids(self, resp):
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        return {str(r["id"]) for r in resp.data["data"]["results"]}

    def _list(self, now, user=None, org=None, **params):
        self.client.force_authenticate(user=user or self.player)
        headers = self._org_headers(org) if org else {}
        with self._at(now):
            return self.client.get(self.LIST_URL, params, **headers)

    # ── the rule itself ──────────────────────────────────────────

    def test_the_rule_is_the_stored_instant_passing(self):
        """
        The whole comparison, and there is no timezone in it. The day
        boundary was resolved when the row was WRITTEN — the end instant
        below is already 23:59:59 IST, because that is this recruitment's
        zone — so the read is just "has it gone by".
        """
        ends_at = self._date_only_trial().trial_end_date
        self.assertEqual(ends_at.astimezone(IST).hour, 23)

        self.assertFalse(trial_window.is_trial_over(ends_at, self.EVENING))
        self.assertTrue(trial_window.is_trial_over(ends_at, self.NEXT_MORNING))

    @override_settings(RECRUITMENT_TIMEZONE="UTC")
    def test_the_setting_cannot_move_an_existing_trial(self):
        """
        RECRUITMENT_TIMEZONE is the default for a NEW ORG and nothing else.

        It used to be the rule for every trial everywhere, which is the bug
        all of this replaced. A row already carries its own zone and its own
        end instant, so changing the setting under it must not move its
        boundary by a second — if this ever fails, something has started
        reading the setting again.
        """
        # Stated explicitly, the way a row written before the setting
        # changed carries it.
        trial = self._date_only_trial(timezone="Asia/Kolkata")

        # 00:30 IST on the 16th is 19:00 UTC on the 15th, so a UTC-wide rule
        # would say this trial's day has NOT ended. It has: the trial is in
        # IST, which is where it is held.
        self.assertTrue(
            trial_window.is_trial_over(
                trial.trial_end_date, now=self.NEXT_MORNING
            )
        )
        self.assertNotIn(
            str(trial.id), self._ids(self._list(self.NEXT_MORNING))
        )

    def test_date_only_trial_is_visible_all_day_and_over_the_next_day(self):
        trial = self._date_only_trial()

        self.assertFalse(trial_window.is_trial_over(trial.event_date, self.EVENING))
        self.assertTrue(
            trial_window.is_trial_over(trial.event_date, self.NEXT_MORNING)
        )

        self.assertIn(str(trial.id), self._ids(self._list(self.EVENING)))
        self.assertNotIn(str(trial.id), self._ids(self._list(self.NEXT_MORNING)))

    def test_timed_trial_stays_visible_after_its_time_until_midnight(self):
        trial = self._timed_trial()  # 09:00 IST

        # 20:00 the same day: the trial's time has passed, the DAY has not.
        self.assertIn(str(trial.id), self._ids(self._list(self.EVENING)))
        # 00:30 the next morning: gone.
        self.assertNotIn(str(trial.id), self._ids(self._list(self.NEXT_MORNING)))

    def test_no_event_date_is_unchanged(self):
        open_ended = self._recruitment("No date")

        self.assertFalse(open_ended.is_trial_over)
        self.assertTrue(open_ended.is_accepting_applications)
        self.assertIn(str(open_ended.id), self._ids(self._list(self.NEXT_MORNING)))

        # The Q helper keeps a null event_date, whatever the clock says.
        kept = Recruitment.objects.filter(
            trial_window.trial_not_over_q(self.NEXT_MORNING)
        )
        self.assertIn(open_ended, kept)

    # ── which lists hide it ──────────────────────────────────────

    def test_owner_list_keeps_ended_trials_other_viewers_do_not(self):
        trial = self._timed_trial()
        now = self.NEXT_MORNING

        # The owning org, on its own username-scoped list.
        owner = self._list(
            now, user=self.owner, org=self.org, username=self.org.username
        )
        self.assertIn(str(trial.id), self._ids(owner))
        row = next(
            r for r in owner.data["data"]["results"] if r["id"] == str(trial.id)
        )
        self.assertTrue(row["is_trial_over"])

        # A player: the ranked All tab, and the org's profile tab.
        self.assertNotIn(str(trial.id), self._ids(self._list(now)))
        self.assertNotIn(
            str(trial.id),
            self._ids(self._list(now, username=self.org.username)),
        )
        # Search too — it goes through the same candidate set.
        self.assertNotIn(
            str(trial.id), self._ids(self._list(now, search="Timed"))
        )

        # Another organization, both on the global list and on the profile tab.
        self.assertNotIn(
            str(trial.id),
            self._ids(self._list(now, user=self.rival, org=self.other_org)),
        )
        self.assertNotIn(
            str(trial.id),
            self._ids(self._list(
                now, user=self.rival, org=self.other_org,
                username=self.org.username,
            )),
        )

    def test_discover_excludes_ended_trials_and_suspended_organizations(self):
        live = self._recruitment("Live")
        ended = self._timed_trial()
        suspended_org = Organization.objects.create(
            name="Banned FC", username="bannedfc",
            type=Organization.Type.CLUB, is_suspended=True,
        )
        from_suspended = self._recruitment("Suspended", org=suspended_org)

        self.client.force_authenticate(user=self.player)
        with self._at(self.NEXT_MORNING):
            resp = self.client.get(self.DISCOVER_URL)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)

        served = {
            str(row["id"])
            for section in SECTION_ORDER
            for row in resp.data["data"].get(section, [])
        }
        self.assertIn(str(live.id), served)
        self.assertNotIn(str(ended.id), served)
        self.assertNotIn(str(from_suspended.id), served)

    def test_discover_keeps_a_trial_later_today(self):
        trial = self._timed_trial()

        self.client.force_authenticate(user=self.player)
        with self._at(self.EVENING):
            resp = self.client.get(self.DISCOVER_URL)

        served = {
            str(row["id"])
            for section in SECTION_ORDER
            for row in resp.data["data"].get(section, [])
        }
        self.assertIn(str(trial.id), served)

    def test_public_organization_bundle_excludes_ended_trials(self):
        live = self._recruitment("Live")
        ended = self._date_only_trial()

        self.client.force_authenticate(user=None)
        with self._at(self.NEXT_MORNING):
            resp = self.client.get(f"/public/organization/{self.org.username}")
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)

        titles = {r["title"] for r in resp.data["data"]["recruitments"]}
        self.assertEqual(titles, {live.title})
        self.assertNotIn(ended.title, titles)

        # ...and the same trial is still advertised while its day is running.
        cache.clear()
        with self._at(self.EVENING):
            resp = self.client.get(f"/public/organization/{self.org.username}")
        titles = {r["title"] for r in resp.data["data"]["recruitments"]}
        self.assertEqual(titles, {live.title, ended.title})

    # ── what still opens, and what is refused ────────────────────

    def test_detail_still_opens_with_is_trial_over_and_apply_is_refused(self):
        trial = self._date_only_trial()

        self.client.force_authenticate(user=self.player)
        with self._at(self.NEXT_MORNING):
            detail = self.client.get(f"/recruitments/{trial.id}/details")
            self.assertEqual(detail.status_code, status.HTTP_200_OK, detail.data)
            self.assertTrue(detail.data["data"]["is_trial_over"])
            self.assertFalse(detail.data["data"]["is_accepting_applications"])
            self.assertFalse(detail.data["data"]["can_apply"])

            # The logged-out link opens too.
            self.client.force_authenticate(user=None)
            public = self.client.get(f"/public/recruitments/{trial.id}")
            self.assertEqual(public.status_code, status.HTTP_200_OK, public.data)
            self.assertTrue(public.data["data"]["is_trial_over"])

            self.client.force_authenticate(user=self.player)
            resp = self.client.post(
                f"/recruitments/{trial.id}/apply",
                {"shared_name": "Player", "shared_phone": "+919876543210"},
                format="json",
            )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST, resp.data)
        self.assertEqual(resp.data["message"], "This trial has ended.")
        self.assertFalse(
            RecruitmentApplication.objects.filter(recruitment=trial).exists()
        )

    def test_a_trial_later_today_still_accepts_applications(self):
        """
        The trial DAY is still the unit for "is it over" — but applications
        close when the trial STARTS, not when its day ends. The two windows
        are different questions (Recruitment.applications_close_at).

        A date-only trial therefore takes applications all day, because 23:59
        is its start; a 09:00 trial takes them until 09:00 and no longer —
        nobody joins a trial that is already underway.
        """
        self.client.force_authenticate(user=self.player)

        date_only = self._date_only_trial()
        with self._at(self.EVENING), self.captureOnCommitCallbacks(execute=True):
            resp = self._apply(date_only)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)

        # No captureOnCommitCallbacks here, unlike the apply above: the
        # post-commit work (notification + email) is the same code either
        # way, and each execution fires off a daemon email thread that
        # outlives this test's transaction.
        timed = self._timed_trial()  # 09:00
        with self._at(self.MORNING):
            resp = self._apply(timed)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)

        # 20:00 — the trial has been running for hours. The day is not over,
        # so this is NOT "this trial has ended"; it is a closed window.
        started = self._timed_trial(title="Already started")
        with self._at(self.EVENING):
            resp = self._apply(started)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST, resp.data)
        self.assertEqual(
            resp.data["message"], "Applications for this trial have closed."
        )

    def _apply(self, trial):
        return self.client.post(
            f"/recruitments/{trial.id}/apply",
            {"shared_name": "Player", "shared_phone": "+919876543210"},
            format="json",
        )

    def test_is_accepting_applications_turns_false_when_the_trial_is_over(self):
        # No deadline set: before this rule such a trial accepted applications
        # indefinitely after the trial itself.
        trial = self._date_only_trial()
        self.assertIsNone(trial.application_deadline)

        with self._at(self.EVENING):
            self.assertFalse(trial.is_trial_over)
            self.assertTrue(trial.is_accepting_applications)

        with self._at(self.NEXT_MORNING):
            self.assertTrue(trial.is_trial_over)
            self.assertFalse(trial.is_accepting_applications)

    # ── the player's own lists keep it ───────────────────────────

    def test_saved_list_keeps_an_ended_trial_flagged(self):
        trial = self._timed_trial()
        self.client.force_authenticate(user=self.player)
        with self._at(self.EVENING):
            on = self.client.post(f"/recruitments/{trial.id}/save")
        self.assertEqual(on.status_code, status.HTTP_200_OK, on.data)

        with self._at(self.NEXT_MORNING):
            resp = self.client.get(SAVED_LIST_URL)
        rows = resp.data["data"]["results"]
        self.assertEqual([r["id"] for r in rows], [str(trial.id)])
        self.assertTrue(rows[0]["is_trial_over"])
        self.assertTrue(rows[0]["is_saved"])

    def test_my_applications_keeps_an_ended_trial_flagged(self):
        trial = self._timed_trial()
        application = RecruitmentApplication.objects.create(
            recruitment=trial, applicant=self.player,
            shared_name="Player", shared_phone="+919876543210",
        )

        self.client.force_authenticate(user=self.player)
        with self._at(self.NEXT_MORNING):
            resp = self.client.get(self.MY_APPS_URL)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)

        rows = resp.data["data"]["results"]
        self.assertEqual([r["id"] for r in rows], [str(application.id)])
        self.assertEqual(rows[0]["recruitment"]["id"], str(trial.id))
        self.assertTrue(rows[0]["recruitment"]["is_trial_over"])

    def test_is_trial_over_adds_no_queries(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        for i in range(3):
            self._timed_trial(title=f"Trial {i}")

        # The property reads a column already on the row: zero queries.
        rows = list(Recruitment.objects.all())
        with self.assertNumQueries(0):
            with self._at(self.NEXT_MORNING):
                self.assertEqual([r.is_trial_over for r in rows], [True] * 3)

        # End to end: the same owner request, once with every flag false and
        # once with every flag true, costs the same number of queries. (Same
        # rows both times, so any per-row cost elsewhere cancels out.)
        self.client.force_authenticate(user=self.owner)
        headers = self._org_headers(self.org)
        # Warm the username→profile lookup cache so the first measured request
        # is not one query dearer than the second for an unrelated reason.
        self.client.get(self.LIST_URL, {"username": self.org.username}, **headers)
        counts = {}
        for label, now in (("live", self.EVENING), ("over", self.NEXT_MORNING)):
            with self._at(now), CaptureQueriesContext(connection) as ctx:
                resp = self.client.get(
                    self.LIST_URL, {"username": self.org.username}, **headers
                )
            self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
            flags = [r["is_trial_over"] for r in resp.data["data"]["results"]]
            self.assertEqual(flags, [label == "over"] * 3)
            counts[label] = len(ctx)

        self.assertEqual(counts["live"], counts["over"])


# ═════════════════════════════════════════════════════════════════════
# Application status split — open-trial date guards, silent statuses, the
# confirmed / selected counters, age_mismatch_at_apply.
# ═════════════════════════════════════════════════════════════════════

from datetime import date

from rest_framework.exceptions import ValidationError

from apps.recruitments.services.application_service import ApplicationService


class ApplicationStatusSplitTests(APITestCase):
    """
    Service-level: change_status / apply / withdraw are called directly, with
    the clock pinned the same way TrialOverTests pins it.
    """

    # Sat 10 Oct 2026, noon IST.
    NOW = datetime(2026, 10, 10, 12, 0, tzinfo=IST)

    def setUp(self):
        self.owner = User.objects.create_user(
            email="own_split@example.com", password="pass1234",
            username="owner_split",
        )
        self.org = Organization.objects.create(
            name="Tide FC", username="tidefc", type=Organization.Type.CLUB
        )
        self.member = OrganizationMember.objects.create(
            organization=self.org, user=self.owner,
            role=OrganizationMember.Role.OWNER,
        )
        self.org_actor = Actor(
            actor_type="organization", organization=self.org,
            organization_member=self.member,
        )
        self.sport = Sport.objects.create(name="Football", icon_name="mdi:soccer")
        self.recruitment = Recruitment.objects.create(
            organization=self.org, sport=self.sport, title="Open Trial",
            status=Recruitment.Status.ACTIVE, recruitment_type="open_trial",
            apply_method="goatza", visibility=Recruitment.Visibility.PUBLIC,
        )
        self._players = 0

    # ── helpers ──────────────────────────────────────────────────

    def _player(self, birthdate=None):
        self._players += 1
        user = User.objects.create_user(
            email=f"split{self._players}@example.com", password="pass1234",
            username=f"split_player{self._players}",
        )
        UserProfile.objects.create(
            user=user, name=f"Player {self._players}", birthdate=birthdate
        )
        return user

    def _app(self, status="applied"):
        return RecruitmentApplication.objects.create(
            recruitment=self.recruitment, applicant=self._player(),
            shared_name="Name", shared_phone="+919876543210", status=status,
        )

    def _change(self, apps, to_status):
        with patch("django.utils.timezone.now", return_value=self.NOW):
            with self.captureOnCommitCallbacks(execute=True):
                return ApplicationService.change_status(
                    self.org_actor, self.recruitment,
                    [str(app.id) for app in apps], to_status,
                )

    def _set_recruitment(self, **fields):
        Recruitment.objects.filter(id=self.recruitment.id).update(**fields)
        self.recruitment.refresh_from_db()

    def _assert_counts(self, confirmed, selected):
        self.recruitment.refresh_from_db()
        self.assertEqual(
            (self.recruitment.confirmed_count, self.recruitment.selected_count),
            (confirmed, selected),
        )

    # ── open-trial results guard ─────────────────────────────────

    def test_results_are_refused_before_the_trial_day(self):
        # Mon 12 Oct, a date-only trial (stored 23:59 local).
        self._set_recruitment(event_date=datetime(2026, 10, 12, 23, 59, tzinfo=IST))
        app = self._app()

        with self.assertRaises(ValidationError) as ctx:
            self._change([app], "selected")

        self.assertEqual(
            str(ctx.exception.detail[0]),
            "Results open on Mon 12 Oct. If that date is wrong, edit the trial.",
        )
        app.refresh_from_db()
        self.assertEqual(app.status, "applied")

    def test_results_open_on_the_trial_day_and_stay_open_after(self):
        on_the_day, after = self._app(), self._app()

        # Later today counts — the unit is the calendar day, not the instant.
        self._set_recruitment(event_date=datetime(2026, 10, 10, 23, 59, tzinfo=IST))
        result = self._change([on_the_day], "selected")
        self.assertEqual(result["updated"], [str(on_the_day.id)])

        self._set_recruitment(event_date=datetime(2026, 10, 8, 9, 0, tzinfo=IST))
        result = self._change([after], "not_selected")
        self.assertEqual(result["updated"], [str(after.id)])

    def test_the_results_guard_is_for_open_trials_only(self):
        self._set_recruitment(
            recruitment_type="player_looking",
            event_date=datetime(2026, 10, 12, 23, 59, tzinfo=IST),
        )
        app = self._app()

        result = self._change([app], "selected")

        self.assertEqual(result["updated"], [str(app.id)])

    # ── silent statuses ──────────────────────────────────────────

    @patch("apps.recruitments.services.application_service.send_application_status_email")
    def test_reviewing_and_shortlisted_notify_nobody(self, mock_email):
        app = self._app()

        self._change([app], "reviewing")
        self._change([app], "shortlisted")

        # The change itself is recorded in full...
        app.refresh_from_db()
        self.assertEqual(app.status, "shortlisted")
        self.assertEqual(app.reviewed_by, self.member)
        self.assertEqual(
            RecruitmentApplicationStatusHistory.objects.filter(
                application=app
            ).count(),
            2,
        )
        # ...but no notification row (so no push either) and no email.
        self.assertFalse(
            Notification.objects.filter(recipient_user=app.applicant).exists()
        )
        mock_email.assert_not_called()

    # ── counters ─────────────────────────────────────────────────

    def test_counters_follow_a_bulk_move_into_and_out_of_trial_confirmed(self):
        a1, a2, a3 = self._app(), self._app(), self._app()

        self._change([a1, a2, a3], "trial_confirmed")
        self._assert_counts(confirmed=3, selected=0)

        self._change([a1, a2], "selected")
        self._assert_counts(confirmed=1, selected=2)

        # a3 is already confirmed (no_change) and must not count twice.
        result = self._change([a1, a3], "trial_confirmed")
        self.assertEqual(result["updated"], [str(a1.id)])
        self._assert_counts(confirmed=2, selected=1)

        # A confirmed player withdrawing gives their count back.
        ApplicationService.withdraw(
            Actor(actor_type="user", user=a3.applicant), a3.id
        )
        self._assert_counts(confirmed=1, selected=1)

    # ── age_mismatch_at_apply ────────────────────────────────────

    def test_age_mismatch_is_recorded_and_an_unknown_age_is_not_one(self):
        u17 = RecruitmentAgeCategory.objects.create(
            recruitment=self.recruitment, title="U17",
            min_birth_year=2009, max_birth_year=2010,
        )
        cases = (
            (self._player(birthdate=date(2004, 5, 1)), True),
            (self._player(birthdate=None), False),
        )

        for player, expected in cases:
            with self.subTest(expected=expected):
                application = ApplicationService.apply(
                    Actor(actor_type="user", user=player),
                    self.recruitment.id,
                    {
                        "shared_name": "Name",
                        "shared_phone": "+919876543210",
                        "age_category": u17.id,
                    },
                )
                application.refresh_from_db()
                self.assertIs(application.age_mismatch_at_apply, expected)


# ═════════════════════════════════════════════════════════════════════
# migrate_recruitment_v3 — the one-off data migration onto the v3 statuses.
# ═════════════════════════════════════════════════════════════════════

from io import StringIO

from django.core.management import call_command

from apps.recruitments.models import RecruitmentBenefit


class MigrateRecruitmentV3Tests(APITestCase):
    """
    selected/rejected are judged by WHEN the org set them (the newest history
    row into the current status) against the trial day — never against today.
    """

    # A date-only trial: stored at 23:59 local on Tue 15 Sep 2026.
    TRIAL = datetime(2026, 9, 15, 23, 59, tzinfo=IST)
    BEFORE = datetime(2026, 9, 10, 12, 0, tzinfo=IST)
    AFTER = datetime(2026, 9, 16, 12, 0, tzinfo=IST)

    def setUp(self):
        owner = User.objects.create_user(
            email="own_v3@example.com", password="pass1234", username="owner_v3"
        )
        self.org = Organization.objects.create(
            name="Harbour FC", username="harbourfc", type=Organization.Type.CLUB
        )
        self.member = OrganizationMember.objects.create(
            organization=self.org, user=owner,
            role=OrganizationMember.Role.OWNER,
        )
        self.sport = Sport.objects.create(name="Football", icon_name="mdi:soccer")
        self.trial = self._recruitment("open_trial", event_date=self.TRIAL)
        self._players = 0

    # ── helpers ──────────────────────────────────────────────────

    def _recruitment(self, recruitment_type, event_date=None):
        return Recruitment.objects.create(
            organization=self.org, sport=self.sport, title="Trial",
            status=Recruitment.Status.ACTIVE, recruitment_type=recruitment_type,
            apply_method="goatza", visibility=Recruitment.Visibility.PUBLIC,
            event_date=event_date,
        )

    def _app(self, recruitment, status):
        self._players += 1
        player = User.objects.create_user(
            email=f"v3_{self._players}@example.com", password="pass1234",
            username=f"v3_player{self._players}",
        )
        return RecruitmentApplication.objects.create(
            recruitment=recruitment, applicant=player,
            shared_name="Name", shared_phone="+919876543210", status=status,
        )

    def _selected(self, recruitment, set_at):
        """An application the org moved to `selected` at ``set_at``."""
        app = self._app(recruitment, "selected")
        history = RecruitmentApplicationStatusHistory.objects.create(
            application=app, from_status="applied", to_status="selected",
            changed_by=self.member,
        )
        # created_at is auto_now_add — backdate it the only way there is.
        RecruitmentApplicationStatusHistory.objects.filter(
            id=history.id
        ).update(created_at=set_at)
        return app

    def _run(self, **options):
        out = StringIO()
        call_command("migrate_recruitment_v3", stdout=out, **options)
        return out.getvalue()

    def _status(self, app):
        app.refresh_from_db()
        return app.status

    def _snapshot(self):
        """Everything the command can write, in a comparable shape."""
        return (
            sorted(RecruitmentApplication.objects.values_list(
                "id", "status", "age_mismatch_at_apply"
            )),
            sorted(RecruitmentApplicationStatusHistory.objects.values_list(
                "id", "to_status"
            )),
            sorted(Recruitment.objects.values_list(
                "id", "recruitment_type", "confirmed_count", "selected_count"
            )),
            sorted(RecruitmentBenefit.objects.values_list("id", "title")),
        )

    def _mixed_fixture(self):
        """One of each thing the command rewrites."""
        self._selected(self.trial, self.BEFORE)
        self._app(self.trial, "invited")
        self._app(self.trial, "rejected")
        self._recruitment("scholarship")

    # ── tests ────────────────────────────────────────────────────

    def test_selected_before_the_trial_day_becomes_trial_confirmed(self):
        app = self._selected(self.trial, self.BEFORE)

        self._run()

        self.assertEqual(self._status(app), "trial_confirmed")
        self.assertTrue(
            RecruitmentApplicationStatusHistory.objects.filter(
                application=app, from_status="selected",
                to_status="trial_confirmed", changed_by=None,
                note="status split migration",
            ).exists()
        )
        self.trial.refresh_from_db()
        self.assertEqual(
            (self.trial.confirmed_count, self.trial.selected_count), (1, 0)
        )

    def test_selected_after_the_trial_day_stays_selected(self):
        app = self._selected(self.trial, self.AFTER)

        self._run()

        self.assertEqual(self._status(app), "selected")
        self.trial.refresh_from_db()
        self.assertEqual(
            (self.trial.confirmed_count, self.trial.selected_count), (0, 1)
        )

    def test_player_looking_keeps_selected_whatever_the_dates(self):
        looking = self._recruitment("player_looking", event_date=self.TRIAL)
        app = self._selected(looking, self.BEFORE)

        self._run()

        self.assertEqual(self._status(app), "selected")

    def test_a_second_run_changes_nothing(self):
        self._mixed_fixture()
        before = self._snapshot()

        self._run()
        after_first = self._snapshot()
        self.assertNotEqual(after_first, before)

        self._run()

        self.assertEqual(self._snapshot(), after_first)

    def test_dry_run_writes_nothing(self):
        self._mixed_fixture()
        before = self._snapshot()

        report = self._run(dry_run=True)

        self.assertEqual(self._snapshot(), before)
        # ...while still reporting what a real run would do.
        self.assertRegex(
            report, r"selected -> trial_confirmed \(set pre-trial\)\s*: 1"
        )


# =====================================================================
# TRIAL SESSIONS - the two windows
# =====================================================================

from datetime import date, time as dt_time

from rest_framework.exceptions import ValidationError as DRFValidationError

from apps.recruitments import trial_window
from apps.recruitments.models import TrialSession
from apps.recruitments.services.application_service import ApplicationService
from apps.recruitments.services.recruitment_service import RecruitmentService


class TrialSessionWindowTests(APITestCase):
    """
    A trial has DATES, and "is this over?" is two questions with two answers:

      is the TRIAL over          trial_end_date — the LAST date's day ending
      are APPLICATIONS open      applications_close_at — the FIRST date in
                                 `all` mode, the last in `choose_one`

    v2 of the spec collapsed them into one, which let somebody apply at 9am
    on the Sunday of a Sat-Sun trial and be auto-confirmed for a trial that
    was half over.

    Every clock here is pinned; the trial dates are IST calendar days.
    """

    # A two-day weekend trial: 12-13 Sep 2026 (Sat-Sun), IST.
    DAY_ONE = date(2026, 9, 12)
    DAY_TWO = date(2026, 9, 13)

    BEFORE = datetime(2026, 9, 10, 10, 0, tzinfo=IST)       # both days ahead
    DAY_TWO_MORNING = datetime(2026, 9, 13, 9, 0, tzinfo=IST)
    AFTER = datetime(2026, 9, 14, 0, 30, tzinfo=IST)        # both days done

    def setUp(self):
        self.owner = self._user("session_owner", "Owner")
        self.player = self._user("session_player", "Player")

        self.org = Organization.objects.create(
            name="Session FC", username="sessionfc",
            type=Organization.Type.CLUB,
        )
        OrganizationMember.objects.create(
            organization=self.org, user=self.owner,
            role=OrganizationMember.Role.OWNER,
        )
        self.sport = Sport.objects.create(
            name="Cricket", icon_name="mdi:cricket"
        )

    # -- factories ------------------------------------------------

    def _user(self, username, name):
        user = User.objects.create_user(
            email=f"{username}@example.com", password="pass1234",
            username=username,
        )
        accept_current_terms(user)
        UserProfile.objects.create(user=user, name=name)
        return user

    def _trial(self, **overrides):
        data = dict(
            organization=self.org, sport=self.sport,
            title="Weekend Trials", recruitment_type="open_trial",
            status=Recruitment.Status.ACTIVE,
            visibility=Recruitment.Visibility.PUBLIC,
        )
        data.update(overrides)
        return Recruitment.objects.create(**data)

    def _session(self, recruitment, day, **overrides):
        return TrialSession.objects.create(
            recruitment=recruitment, date=day, **overrides
        )

    def _sync(self, recruitment):
        RecruitmentService._sync_trial_window(recruitment)
        recruitment.refresh_from_db()
        return recruitment

    def _at(self, now):
        return patch("django.utils.timezone.now", return_value=now)

    # -- 1. the window is derived from the dates ------------------

    def test_window_is_derived_from_first_and_last_session(self):
        trial = self._trial()
        self._session(trial, self.DAY_TWO, start_time=dt_time(14, 0))
        self._session(trial, self.DAY_ONE, start_time=dt_time(9, 0))

        self._sync(trial)

        # event_date is day ONE at its own start time...
        self.assertEqual(
            trial.event_date.astimezone(IST),
            datetime(2026, 9, 12, 9, 0, tzinfo=IST),
        )
        # ...and the window closes at the end of day TWO, whatever time that
        # session started.
        self.assertEqual(
            trial.trial_end_date.astimezone(IST),
            datetime(2026, 9, 13, 23, 59, 59, tzinfo=IST),
        )

    # -- 2. a cancelled date drops out of the window --------------

    def test_cancelling_the_first_session_moves_event_date_on(self):
        trial = self._trial()
        day_one = self._session(trial, self.DAY_ONE, start_time=dt_time(9, 0))
        self._session(trial, self.DAY_TWO, start_time=dt_time(14, 0))
        self._sync(trial)

        day_one.is_cancelled = True
        day_one.save(update_fields=["is_cancelled"])
        self._sync(trial)

        self.assertEqual(
            trial.event_date.astimezone(IST),
            datetime(2026, 9, 13, 14, 0, tzinfo=IST),
        )

        # Cancel the rest and BOTH columns go back to null — there is no
        # trial left to have a window.
        trial.sessions.update(is_cancelled=True)
        self._sync(trial)

        self.assertIsNone(trial.event_date)
        self.assertIsNone(trial.trial_end_date)

    # -- 3. `all` closes on day one -------------------------------

    def test_all_mode_closes_applications_on_the_first_date(self):
        trial = self._trial(session_mode=Recruitment.SessionMode.ALL)
        self._session(trial, self.DAY_ONE, start_time=dt_time(9, 0))
        self._session(trial, self.DAY_TWO, start_time=dt_time(9, 0))
        self._sync(trial)

        self.assertEqual(trial.applications_close_at, trial.event_date)

        # Sunday morning: the trial is still running, but nobody new may join
        # a two-day trial on day two.
        with self._at(self.DAY_TWO_MORNING):
            self.assertFalse(trial.is_accepting_applications)
            self.assertFalse(trial.is_trial_over)

            with self.assertRaises(DRFValidationError) as caught:
                ApplicationService.apply(
                    Actor(actor_type="user", user=self.player),
                    trial.id,
                    {"shared_name": "P", "shared_phone": "9999999999"},
                )

        self.assertIn(
            "Applications for this trial have closed.",
            str(caught.exception.detail),
        )

    # -- 4. choose_one stays open to the last date ----------------

    def test_choose_one_stays_open_until_the_last_date(self):
        trial = self._trial(
            session_mode=Recruitment.SessionMode.CHOOSE_ONE
        )
        self._session(trial, self.DAY_ONE, start_time=dt_time(9, 0))
        day_two = self._session(
            trial, self.DAY_TWO, start_time=dt_time(9, 0)
        )
        self._sync(trial)

        self.assertEqual(trial.applications_close_at, trial.trial_end_date)

        # Same Sunday morning — still open, because each city is its own round.
        with self._at(self.DAY_TWO_MORNING):
            self.assertTrue(trial.is_accepting_applications)

            application = ApplicationService.apply(
                Actor(actor_type="user", user=self.player),
                trial.id,
                {
                    "shared_name": "P",
                    "shared_phone": "9999999999",
                    "session": day_two.id,
                },
            )

        self.assertEqual(application.session_id, day_two.id)

        with self._at(self.AFTER):
            self.assertFalse(trial.is_accepting_applications)

    # -- 5. over means the WHOLE window is over -------------------

    def test_is_trial_over_reads_the_last_weekend_not_the_first(self):
        trial = self._trial()
        for week in range(3):
            self._session(trial, self.DAY_ONE + timedelta(days=7 * week))
        self._sync(trial)

        # Weekend one has been and gone; two more to run.
        with self._at(datetime(2026, 9, 14, 10, 0, tzinfo=IST)):
            self.assertFalse(trial.is_trial_over)
            self.assertIn(
                trial.id,
                Recruitment.objects.filter(
                    trial_window.trial_not_over_q()
                ).values_list("id", flat=True),
            )

        # The morning after the third.
        with self._at(datetime(2026, 9, 27, 0, 30, tzinfo=IST)):
            self.assertTrue(trial.is_trial_over)
            self.assertNotIn(
                trial.id,
                Recruitment.objects.filter(
                    trial_window.trial_not_over_q()
                ).values_list("id", flat=True),
            )

    # -- 6. editing dates keeps the applicants' choice ------------

    def test_editing_sessions_keeps_an_applicants_chosen_date(self):
        trial = self._trial(
            session_mode=Recruitment.SessionMode.CHOOSE_ONE
        )
        kochi = self._session(
            trial, self.DAY_ONE, title="Kochi", start_time=dt_time(9, 0)
        )
        calicut = self._session(trial, self.DAY_TWO, title="Calicut")
        self._sync(trial)

        application = RecruitmentApplication.objects.create(
            recruitment=trial, applicant=self.player,
            shared_name="P", shared_phone="9999999999",
            session=kochi,
        )

        # The org fixes a typo in one title and adds a third city. Every row
        # it still wants comes back carrying its id.
        RecruitmentService._sync_trial_sessions(trial, [
            {
                "id": kochi.id, "title": "Kochi round",
                "date": self.DAY_ONE, "start_time": dt_time(9, 0),
                "display_order": 0,
            },
            {
                "id": calicut.id, "title": "Calicut",
                "date": self.DAY_TWO, "display_order": 1,
            },
            {"title": "Kannur", "date": date(2026, 9, 20), "display_order": 2},
        ])
        self._sync(trial)

        application.refresh_from_db()
        kochi.refresh_from_db()

        self.assertEqual(application.session_id, kochi.id)
        self.assertEqual(kochi.title, "Kochi round")
        self.assertEqual(trial.sessions.count(), 3)
        self.assertEqual(
            trial.trial_end_date.astimezone(IST).date(), date(2026, 9, 20)
        )

    # -- 7. a date from another trial is not adopted --------------

    def test_choose_one_rejects_a_session_from_another_recruitment(self):
        trial = self._trial(
            session_mode=Recruitment.SessionMode.CHOOSE_ONE
        )
        self._session(trial, self.DAY_ONE, start_time=dt_time(9, 0))
        self._session(trial, self.DAY_TWO, start_time=dt_time(9, 0))
        self._sync(trial)

        other = self._trial(title="Someone else's trial")
        stolen = self._session(other, self.DAY_ONE)
        self._sync(other)

        actor = Actor(actor_type="user", user=self.player)
        payload = {"shared_name": "P", "shared_phone": "9999999999"}

        with self._at(self.BEFORE):
            # An id this trial does not own is REJECTED, never adopted...
            with self.assertRaises(DRFValidationError) as caught:
                ApplicationService.apply(
                    actor, trial.id, {**payload, "session": stolen.id}
                )
            self.assertIn(
                "Invalid date for this recruitment.",
                str(caught.exception.detail),
            )

            # ...and on a choose_one trial a date is not optional either.
            with self.assertRaises(DRFValidationError) as caught:
                ApplicationService.apply(actor, trial.id, payload)

        self.assertIn(
            "Pick which date you'll attend.", str(caught.exception.detail)
        )
        self.assertFalse(
            RecruitmentApplication.objects.filter(recruitment=trial).exists()
        )


# =====================================================================
# TRIAL SESSIONS - the payload shape every client reads
# =====================================================================

from rest_framework import serializers

from apps.recruitments.serializers.recruitment_list_serializers import (
    RecruitmentDetailSerializer,
    RecruitmentListSerializer,
    RecruitmentOwnerDetailSerializer,
    TrialSessionsMixin,
)


class TrialSessionsPayloadShapeTests(APITestCase):
    """
    `sessions` is a LIST OF DATES, not a list of ids.

    TrialSessionsMixin declares it as a SerializerMethodField, but DRF only
    collects declared fields off a base that carries `_declared_fields` -
    which only SerializerMetaclass puts there. When the mixin was a plain
    class every one of its fields was dropped, and `sessions` did not then
    go missing: ModelSerializer saw the reverse relation and quietly built a
    PrimaryKeyRelatedField(many=True). Every recruitment payload shipped bare
    UUIDs, the detail page threw on the first one, and the edit wizard
    prefilled a trial with no dates at all.

    So this asserts the SHAPE, on every serializer that carries the mixin.
    A field that silently downgrades to the model default is exactly the
    kind of break no amount of business-logic testing catches.
    """

    SERIALIZERS = (
        RecruitmentListSerializer,
        RecruitmentDetailSerializer,
        RecruitmentOwnerDetailSerializer,
    )

    def setUp(self):
        self.org = Organization.objects.create(
            name="Shape FC", username="shapefc",
            type=Organization.Type.CLUB,
        )
        self.sport = Sport.objects.create(
            name="Hockey", icon_name="mdi:hockey-sticks"
        )
        self.trial = Recruitment.objects.create(
            organization=self.org, sport=self.sport,
            title="City tour", recruitment_type="open_trial",
            status=Recruitment.Status.ACTIVE,
            visibility=Recruitment.Visibility.PUBLIC,
            session_mode=Recruitment.SessionMode.CHOOSE_ONE,
            venue_name="Corporation Stadium", city="Kozhikode",
        )
        self.first = TrialSession.objects.create(
            recruitment=self.trial, date=date(2026, 10, 10),
            start_time=dt_time(9, 0), end_time=dt_time(13, 0),
            city="Kochi",
        )
        TrialSession.objects.create(
            recruitment=self.trial, date=date(2026, 10, 12),
            start_time=dt_time(9, 0), city="Kannur",
        )
        RecruitmentService._sync_trial_window(self.trial)
        self.trial.refresh_from_db()

    def test_mixin_fields_survive_onto_every_serializer(self):
        """Declared, not rebuilt from the model."""
        for serializer_class in self.SERIALIZERS:
            fields = serializer_class().fields
            with self.subTest(serializer=serializer_class.__name__):
                for name in TrialSessionsMixin.SESSION_FIELDS:
                    self.assertIn(name, fields)
                self.assertIsInstance(
                    fields["sessions"], serializers.SerializerMethodField
                )

    def test_sessions_are_objects_with_the_fields_the_client_reads(self):
        for serializer_class in self.SERIALIZERS:
            data = serializer_class(self.trial).data
            with self.subTest(serializer=serializer_class.__name__):
                sessions = data["sessions"]
                self.assertEqual(len(sessions), 2)

                for session in sessions:
                    self.assertIsInstance(session, dict)

                # Ordered, and every field the date list renders is present -
                # `end_time` included, which is the one the client probes for.
                opening = sessions[0]
                self.assertEqual(str(opening["id"]), str(self.first.id))
                self.assertEqual(str(opening["date"]), "2026-10-10")
                self.assertEqual(str(opening["start_time"]), "09:00:00")
                self.assertEqual(str(opening["end_time"]), "13:00:00")
                self.assertFalse(opening["is_cancelled"])
                self.assertEqual(opening["city"], "Kochi")
                # A date that set no venue inherits the trial's own, so
                # nothing downstream has to fall back.
                self.assertEqual(
                    opening["venue_name"], "Corporation Stadium"
                )

                self.assertIsNone(sessions[1]["end_time"])

    def test_trial_window_fields_ride_along(self):
        """The two windows are on the payload, and they are not the same."""
        data = RecruitmentOwnerDetailSerializer(self.trial).data

        self.assertEqual(data["session_mode"], "choose_one")
        self.assertIsNotNone(data["trial_end_date"])
        # choose_one: applications close with the LAST date, not the first.
        self.assertIsNotNone(data["applications_close_at"])


# =====================================================================
# PER-TRIAL SETTINGS - auto-confirm, the fee, age filters
# =====================================================================

from apps.recruitments.serializers.recruitment_serializers import (
    RecruitmentCreateSerializer,
)
from apps.recruitments.selectors.application_selectors import (
    ApplicationSelector,
)


class TrialSettingsTests(APITestCase):
    """
    The per-trial settings an org actually asked for.

    auto_confirm    everyone who applies gets their pass on the spot - but
                    NOT after applications close, which is the pairing the
                    two windows were separated for in the first place
    the fee         information, never a gate
    birth years     how old an applicant IS, which is a different question
                    from the age group they applied UNDER
    """

    # Far enough ahead that no clock needs pinning: the window is open and
    # the trial is not over, whenever these run.
    TRIAL_DATE = date(2030, 6, 15)
    SECOND_DATE = date(2030, 6, 16)

    def setUp(self):
        self.owner = self._user("settings_owner", "Owner")
        self.org = Organization.objects.create(
            name="Settings FC", username="settingsfc",
            type=Organization.Type.CLUB,
        )
        self.member = OrganizationMember.objects.create(
            organization=self.org, user=self.owner,
            role=OrganizationMember.Role.OWNER,
        )
        self.sport = Sport.objects.create(
            name="Hockey", icon_name="mdi:hockey-sticks"
        )
        self.org_actor = Actor(
            actor_type="organization",
            organization=self.org,
            organization_member=self.member,
        )

    # -- factories ------------------------------------------------

    def _user(self, username, name, birthdate=None):
        user = User.objects.create_user(
            email=f"{username}@example.com", password="pass1234",
            username=username,
        )
        accept_current_terms(user)
        UserProfile.objects.create(user=user, name=name, birthdate=birthdate)
        return user

    def _trial(self, dates=(TRIAL_DATE,), **overrides):
        data = dict(
            organization=self.org, sport=self.sport,
            title="Settings Trial", recruitment_type="open_trial",
            status=Recruitment.Status.ACTIVE,
            visibility=Recruitment.Visibility.PUBLIC,
        )
        data.update(overrides)
        recruitment = Recruitment.objects.create(**data)
        for day in dates:
            TrialSession.objects.create(recruitment=recruitment, date=day)
        RecruitmentService._sync_trial_window(recruitment)
        recruitment.refresh_from_db()
        return recruitment

    def _apply(self, recruitment, user):
        return ApplicationService.apply(
            Actor(actor_type="user", user=user),
            recruitment.id,
            {"shared_name": "P", "shared_phone": "9999999999"},
        )

    def _application(self, recruitment, user, **overrides):
        data = dict(
            recruitment=recruitment, applicant=user,
            shared_name="P", shared_phone="9999999999",
        )
        data.update(overrides)
        return RecruitmentApplication.objects.create(**data)

    # -- 1. auto-confirm lands confirmed --------------------------

    def test_auto_confirm_lands_the_application_confirmed(self):
        trial = self._trial(auto_confirm=True)
        player = self._user("auto_player", "Auto")

        application = self._apply(trial, player)

        self.assertEqual(
            application.status,
            RecruitmentApplication.Status.TRIAL_CONFIRMED,
        )

        history = application.status_history.get()
        self.assertEqual(history.from_status, "")
        self.assertEqual(
            history.to_status, RecruitmentApplication.Status.TRIAL_CONFIRMED
        )
        self.assertIsNone(history.changed_by)
        self.assertEqual(history.note, "auto-confirmed")

        trial.refresh_from_db()
        self.assertEqual(trial.applications_count, 1)
        self.assertEqual(trial.confirmed_count, 1)

        # ...and with it off, nothing changes about the old behaviour.
        plain = self._trial()
        plain_application = self._apply(plain, self._user("plain", "Plain"))
        plain.refresh_from_db()

        self.assertEqual(
            plain_application.status, RecruitmentApplication.Status.APPLIED
        )
        self.assertEqual(plain.confirmed_count, 0)

    # -- 2. ...but it is not a way past the closed window ---------

    def test_auto_confirm_does_not_bypass_the_application_window(self):
        trial = self._trial(
            auto_confirm=True,
            application_deadline=datetime(2030, 6, 1, 12, 0, tzinfo=IST),
        )
        player = self._user("late_player", "Late")

        # The deadline has passed; the trial itself has not started.
        with patch(
            "django.utils.timezone.now",
            return_value=datetime(2030, 6, 10, 9, 0, tzinfo=IST),
        ):
            with self.assertRaises(DRFValidationError) as caught:
                self._apply(trial, player)

        self.assertIn(
            "The application deadline has passed.",
            str(caught.exception.detail),
        )
        self.assertFalse(
            RecruitmentApplication.objects.filter(recruitment=trial).exists()
        )

        trial.refresh_from_db()
        self.assertEqual(trial.confirmed_count, 0)

    # -- 3. birth years, not ages ---------------------------------

    def test_birth_year_range_filters_and_reports_who_it_hid(self):
        trial = self._trial()

        self._application(
            trial, self._user("born_2010", "A", date(2010, 3, 1))
        )
        self._application(
            trial, self._user("born_2012", "B", date(2012, 9, 20))
        )
        undated = self._application(
            trial, self._user("no_birthdate", "C")
        )

        page, total, no_birth_year = ApplicationSelector.list_applications(
            recruitment=trial, birth_year_min=2011, birth_year_max=2013
        )

        self.assertEqual(total, 1)
        self.assertEqual(
            [a.applicant.username for a in page], ["born_2012"]
        )
        # The applicant with no birthdate is excluded - and COUNTED, or the
        # org never learns the range hid somebody.
        self.assertEqual(no_birth_year, 1)
        self.assertNotIn(
            undated.id, [a.id for a in page]
        )

        # No range active: nothing is hidden, so nothing is reported.
        page, total, no_birth_year = ApplicationSelector.list_applications(
            recruitment=trial
        )
        self.assertEqual(total, 3)
        self.assertEqual(no_birth_year, 0)

        # Sorted by birth year, the unknown sorts LAST either way.
        for sort in ("birth_year", "-birth_year"):
            page, _, _ = ApplicationSelector.list_applications(
                recruitment=trial, sort=sort
            )
            self.assertEqual(
                [a.applicant.username for a in page][-1], "no_birthdate"
            )



# =====================================================================
# ANNOUNCEMENTS - the request writes, the cron sends
# =====================================================================

from django.core.management import call_command

from apps.notifications.models import Notification as NotificationModel
from apps.recruitments.models import (
    AnnouncementDelivery,
    RecruitmentAnnouncement,
)
from apps.recruitments.services.announcement_service import (
    MAX_PER_DAY,
    AnnouncementService,
)


class AnnouncementOutboxTests(APITestCase):
    """
    An announcement is WRITTEN by the request and SENT by a cron job, and
    that split is the feature, not an implementation detail: sending to 340
    people from a web request would be 340 daemon email threads and 340
    inline FCM calls (see AnnouncementDelivery's docstring).

    So the first test here asserts a NEGATIVE - that creating one sends
    nothing at all - and the rest check that the outbox drains exactly once.
    """

    TRIAL_DATE = date(2030, 7, 12)
    SECOND_DATE = date(2030, 7, 19)

    def setUp(self):
        self.owner = self._user("ann_owner", "Owner")
        self.org = Organization.objects.create(
            name="Announce FC", username="announcefc",
            type=Organization.Type.CLUB,
        )
        self.member = OrganizationMember.objects.create(
            organization=self.org, user=self.owner,
            role=OrganizationMember.Role.OWNER,
        )
        self.sport = Sport.objects.create(name="Rugby", icon_name="mdi:rugby")
        self.org_actor = Actor(
            actor_type="organization",
            organization=self.org,
            organization_member=self.member,
        )

    # -- factories ------------------------------------------------

    def _user(self, username, name, email=None):
        user = User.objects.create_user(
            email=(f"{username}@example.com" if email is None else email),
            password="pass1234",
            username=username,
        )
        accept_current_terms(user)
        UserProfile.objects.create(user=user, name=name)
        return user

    def _trial(self, dates=(TRIAL_DATE,), **overrides):
        data = dict(
            organization=self.org, sport=self.sport,
            title="Announce Trial", recruitment_type="open_trial",
            status=Recruitment.Status.ACTIVE,
            visibility=Recruitment.Visibility.PUBLIC,
        )
        data.update(overrides)
        recruitment = Recruitment.objects.create(**data)
        for day in dates:
            TrialSession.objects.create(recruitment=recruitment, date=day)
        RecruitmentService._sync_trial_window(recruitment)
        recruitment.refresh_from_db()
        return recruitment

    def _application(self, recruitment, user, **overrides):
        data = dict(
            recruitment=recruitment, applicant=user,
            shared_name="P", shared_phone="9999999999",
        )
        data.update(overrides)
        return RecruitmentApplication.objects.create(**data)

    def _payload(self, **overrides):
        data = {
            "title": "Venue has changed",
            "body": "We've moved to Corporation Stadium, Gate 3.",
            "audience": RecruitmentAnnouncement.Audience.ALL_APPLICANTS,
        }
        data.update(overrides)
        return data

    def _create(self, recruitment, **overrides):
        return AnnouncementService.create(
            self.org_actor, recruitment, self._payload(**overrides)
        )

    # -- 1. it writes rows and sends NOTHING ----------------------

    def test_create_writes_the_outbox_and_sends_nothing(self):
        trial = self._trial()
        self._application(trial, self._user("ann_a", "A"))
        self._application(trial, self._user("ann_b", "B"))

        with patch(
            "utils.emails.send_email_async"
        ) as async_email, patch(
            "utils.emails.send_email"
        ) as blocking_email:
            announcement = self._create(trial)

        # THE POINT OF THE WHOLE DESIGN: not one email, by either path...
        async_email.assert_not_called()
        blocking_email.assert_not_called()
        # ...and not one notification row.
        self.assertFalse(
            NotificationModel.objects.filter(
                type=NotificationModel.Type.RECRUITMENT_ANNOUNCEMENT
            ).exists()
        )

        # ...and not one Goatza message either: the dm channel is written
        # here and sent by the drain, like everything else.
        self.assertFalse(Message.objects.exists())

        # What it DID write: one row per person per channel, all pending.
        self.assertEqual(announcement.recipients_count, 2)
        deliveries = announcement.deliveries.all()
        self.assertEqual(deliveries.count(), 6)
        self.assertEqual(
            set(deliveries.values_list("channel", flat=True)),
            {"dm", "notification", "email"},
        )
        self.assertEqual(
            set(deliveries.values_list("state", flat=True)), {"pending"}
        )

    # -- 2. who each audience means -------------------------------

    def test_each_audience_resolves_to_its_own_people(self):
        trial = self._trial()

        applied = self._application(trial, self._user("aud_applied", "A"))
        confirmed = self._application(
            trial, self._user("aud_confirmed", "C"),
            status=RecruitmentApplication.Status.TRIAL_CONFIRMED,
        )
        selected = self._application(
            trial, self._user("aud_selected", "S"),
            status=RecruitmentApplication.Status.SELECTED,
        )
        not_selected = self._application(
            trial, self._user("aud_not_selected", "N"),
            status=RecruitmentApplication.Status.NOT_SELECTED,
        )
        # Withdrew: on nobody's list, not even "all applicants".
        self._application(
            trial, self._user("aud_withdrawn", "W"),
            status=RecruitmentApplication.Status.WITHDRAWN,
        )

        def ids(audience):
            return set(
                AnnouncementService.audience_queryset(
                    trial, audience
                ).values_list("id", flat=True)
            )

        self.assertEqual(
            ids(RecruitmentAnnouncement.Audience.ALL_APPLICANTS),
            {applied.id, confirmed.id, selected.id, not_selected.id},
        )
        # Everyone CALLED to the trial - including the people already given a
        # result, who are still on the day's list.
        self.assertEqual(
            ids(RecruitmentAnnouncement.Audience.CONFIRMED),
            {confirmed.id, selected.id, not_selected.id},
        )
        self.assertEqual(
            ids(RecruitmentAnnouncement.Audience.SELECTED), {selected.id}
        )

    # -- 3. narrowing to one date ---------------------------------

    def test_a_session_narrows_the_audience_to_that_date(self):
        trial = self._trial(
            dates=(self.TRIAL_DATE, self.SECOND_DATE),
            session_mode=Recruitment.SessionMode.CHOOSE_ONE,
        )
        kochi, calicut = list(trial.sessions.order_by("date"))

        here = self._application(
            trial, self._user("sess_here", "H"), session=kochi
        )
        self._application(
            trial, self._user("sess_there", "T"), session=calicut
        )

        announcement = self._create(trial, session=kochi)

        self.assertEqual(announcement.session_id, kochi.id)
        self.assertEqual(announcement.recipients_count, 1)
        self.assertEqual(
            list(announcement.deliveries.values_list(
                "application_id", flat=True
            ).distinct()),
            [here.id],
        )

    # -- 4. the drain sends once, and only once -------------------

    def test_the_drain_sends_each_row_once(self):
        trial = self._trial()
        self._application(trial, self._user("drain_a", "A"))
        announcement = self._create(trial)

        with patch(
            "apps.recruitments.management.commands."
            "dispatch_announcements.send_announcement_email",
            return_value=True,
        ) as send_email:
            call_command("dispatch_announcements", verbosity=0)

        self.assertEqual(send_email.call_count, 1)
        self.assertEqual(
            set(announcement.deliveries.values_list("state", flat=True)),
            {"sent"},
        )
        for delivery in announcement.deliveries.all():
            self.assertIsNotNone(delivery.sent_at)
            self.assertEqual(delivery.attempts, 1)

        # The notification went out through the ordinary service path.
        self.assertEqual(
            NotificationModel.objects.filter(
                type=NotificationModel.Type.RECRUITMENT_ANNOUNCEMENT
            ).count(),
            1,
        )

        # A SECOND RUN IS A NO-OP. Only PENDING rows are claimed and SENT is
        # terminal, so nobody is emailed twice.
        with patch(
            "apps.recruitments.management.commands."
            "dispatch_announcements.send_announcement_email",
            return_value=True,
        ) as second_send:
            call_command("dispatch_announcements", verbosity=0)

        second_send.assert_not_called()
        self.assertEqual(
            NotificationModel.objects.filter(
                type=NotificationModel.Type.RECRUITMENT_ANNOUNCEMENT
            ).count(),
            1,
        )
        for delivery in announcement.deliveries.all():
            self.assertEqual(delivery.attempts, 1)

    # -- 5. a skip is WRITTEN, never omitted ----------------------

    def test_a_recipient_with_no_email_gets_a_skipped_row(self):
        trial = self._trial()
        # A phone-only account: real on this product (signup takes either),
        # and the one recipient an email channel genuinely cannot reach. The
        # row still has to exist, or the delivery summary stops adding up to
        # recipients_count.
        no_email = User.objects.create_user(
            phone="+919876500000", password="pass1234",
            username="no_email_player",
        )
        accept_current_terms(no_email)
        UserProfile.objects.create(user=no_email, name="No Email")
        self._application(trial, no_email)

        announcement = self._create(trial)

        self.assertEqual(announcement.recipients_count, 1)
        self.assertEqual(announcement.deliveries.count(), 3)
        self.assertEqual(
            announcement.deliveries.get(channel="email").state, "skipped"
        )
        # The other two channels are unaffected - no address is not no
        # account, and a Goatza message needs neither.
        self.assertEqual(
            announcement.deliveries.get(channel="notification").state,
            "pending",
        )
        self.assertEqual(
            announcement.deliveries.get(channel="dm").state, "pending"
        )

        summary = AnnouncementService.delivery_summaries([announcement.id])
        self.assertEqual(summary[str(announcement.id)]["skipped"], 1)
        self.assertEqual(summary[str(announcement.id)]["pending"], 2)

    # -- 6. the daily cap -----------------------------------------

    def test_the_sixth_announcement_in_a_day_is_refused(self):
        trial = self._trial()
        self._application(trial, self._user("cap_a", "A"))

        for index in range(MAX_PER_DAY):
            self._create(trial, title=f"Update {index}")

        with self.assertRaises(DRFValidationError) as caught:
            self._create(trial, title="One too many")

        self.assertIn("You can send more from midnight", str(caught.exception.detail))
        self.assertEqual(
            RecruitmentAnnouncement.objects.filter(recruitment=trial).count(),
            MAX_PER_DAY,
        )

        # Soft-deleting one does NOT buy another send: it was still sent.
        AnnouncementService.delete(
            self.org_actor,
            RecruitmentAnnouncement.objects.filter(recruitment=trial).first(),
        )
        with self.assertRaises(DRFValidationError):
            self._create(trial, title="Still one too many")


# =====================================================================
# GOATZA DM FAN-OUT, REMINDERS, THE PASS
# =====================================================================

from apps.messaging.models import Conversation, ConversationParticipant, Message
from apps.messaging.services.conversation_service import ConversationService
from apps.moderation.models import Block
from apps.moderation.services.block_guard import BlockedError
from apps.recruitments.services.recruitment_message_service import (
    RecruitmentMessageService,
)
from apps.recruitments.pass_code import mint_for


class RecruitmentMessagingTests(APITestCase):
    """
    Trial updates reach players INSIDE Goatza.

    The load-bearing bit is ``force_active``: without it the org's first
    message to an applicant who does not follow them lands in a message
    REQUEST folder, and an update saying the venue moved that nobody opens
    has failed at the only job it had. Applying to the trial is the consent
    signal that earns the bypass — and a block still outranks it.
    """

    TRIAL_DATE = date(2030, 9, 14)

    def setUp(self):
        self.owner = self._user("dm_owner", "Owner")
        self.player = self._user("dm_player", "Player")
        self.org = Organization.objects.create(
            name="DM FC", username="dmfc", type=Organization.Type.CLUB,
        )
        self.member = OrganizationMember.objects.create(
            organization=self.org, user=self.owner,
            role=OrganizationMember.Role.OWNER,
        )
        self.sport = Sport.objects.create(name="Tennis", icon_name="mdi:tennis")
        self.org_actor = Actor(
            actor_type="organization",
            organization=self.org,
            organization_member=self.member,
        )
        self.trial = self._trial()
        self.application = self._application(self.trial, self.player)

    def _user(self, username, name):
        user = User.objects.create_user(
            email=f"{username}@example.com", password="pass1234",
            username=username,
        )
        accept_current_terms(user)
        UserProfile.objects.create(user=user, name=name)
        return user

    def _trial(self, dates=(TRIAL_DATE,), **overrides):
        data = dict(
            organization=self.org, sport=self.sport, title="DM Trial",
            recruitment_type="open_trial",
            status=Recruitment.Status.ACTIVE,
            visibility=Recruitment.Visibility.PUBLIC,
        )
        data.update(overrides)
        recruitment = Recruitment.objects.create(**data)
        for day in dates:
            TrialSession.objects.create(recruitment=recruitment, date=day)
        RecruitmentService._sync_trial_window(recruitment)
        recruitment.refresh_from_db()
        return recruitment

    def _application(self, recruitment, user, **overrides):
        data = dict(
            recruitment=recruitment, applicant=user,
            shared_name="P", shared_phone="9999999999",
        )
        data.update(overrides)
        return RecruitmentApplication.objects.create(**data)

    # -- 1. the request gate, lifted ------------------------------

    def test_force_active_opens_a_thread_the_gate_would_have_held(self):
        # No mutual follow, so the ordinary path makes a REQUEST.
        plain, _ = ConversationService.get_or_create_conversation(
            actor_org=self.org, target_user=self._user("dm_other", "Other"),
        )
        self.assertEqual(plain.status, Conversation.Status.REQUESTED)
        self.assertEqual(
            sorted(
                plain.participants.values_list("has_accepted", flat=True)
            ),
            [False, True],
        )

        # Same relationship, but this player APPLIED — so the thread opens.
        conversation, _ = ConversationService.get_or_create_conversation(
            actor_org=self.org, target_user=self.player, force_active=True,
        )
        self.assertEqual(conversation.status, Conversation.Status.ACTIVE)
        self.assertEqual(
            list(
                conversation.participants.values_list("has_accepted", flat=True)
            ),
            [True, True],
        )

        # ...and an ALREADY-REQUESTED thread is lifted, not left behind.
        stale = self._user("dm_stale", "Stale")
        requested, _ = ConversationService.get_or_create_conversation(
            actor_org=self.org, target_user=stale,
        )
        self.assertEqual(requested.status, Conversation.Status.REQUESTED)

        lifted, created = ConversationService.get_or_create_conversation(
            actor_org=self.org, target_user=stale, force_active=True,
        )
        self.assertFalse(created)
        self.assertEqual(lifted.id, requested.id)
        lifted.refresh_from_db()
        self.assertEqual(lifted.status, Conversation.Status.ACTIVE)
        self.assertTrue(
            ConversationParticipant.objects
            .get(conversation=lifted, user=stale).has_accepted
        )

    # -- 2. a block still wins ------------------------------------

    def test_force_active_does_not_bypass_the_block_guard(self):
        Block.objects.create(blocker_user=self.player, blocked_org=self.org)

        with self.assertRaises(BlockedError):
            ConversationService.get_or_create_conversation(
                actor_org=self.org,
                target_user=self.player,
                force_active=True,
            )

        # ...and the fan-out reports that ONE recipient as skipped rather
        # than failing the whole send.
        sent, skipped = RecruitmentMessageService.fan_out(
            self.trial, [self.application],
            body="Venue moved", message_type=Message.Type.TEXT,
        )
        self.assertEqual(sent, [])
        self.assertEqual(len(skipped), 1)
        self.assertFalse(Message.objects.exists())

    # -- 3. the dm channel writes a real card message -------------

    def test_the_dm_channel_sends_a_shared_recruitment_message(self):
        announcement = AnnouncementService.create(
            self.org_actor, self.trial,
            {
                "title": "Venue has changed",
                "body": "Gate 3.",
                "audience": RecruitmentAnnouncement.Audience.ALL_APPLICANTS,
                "session": None,
            },
        )

        # A dm row was queued alongside the other two channels...
        self.assertEqual(
            set(announcement.deliveries.values_list("channel", flat=True)),
            {"dm", "notification", "email"},
        )
        # ...and nothing was sent by the request.
        self.assertFalse(Message.objects.exists())

        with patch(
            "apps.recruitments.management.commands."
            "dispatch_announcements.send_announcement_email",
            return_value=True,
        ):
            call_command("dispatch_announcements", verbosity=0)

        message = Message.objects.get()
        self.assertEqual(message.message_type, Message.Type.SHARED_RECRUITMENT)
        # The FK the DB CheckConstraint ties to that type, and every other
        # shared_* column null.
        self.assertEqual(message.shared_recruitment_id, self.trial.id)
        self.assertIsNone(message.shared_post_id)
        self.assertIsNone(message.shared_profile_user_id)
        self.assertIsNone(message.shared_profile_org_id)
        self.assertEqual(message.sender_org_id, self.org.id)
        self.assertIn("Venue has changed", message.content)

        # The thread it landed in is ACTIVE, not a request.
        self.assertEqual(
            message.conversation.status, Conversation.Status.ACTIVE
        )

    # -- 4. message selected queues, and sends nothing ------------

    def test_message_selected_queues_rows_and_sends_nothing(self):
        with patch("utils.emails.send_email_async") as async_email:
            result = AnnouncementService.create_direct(
                self.org_actor, self.trial,
                [self.application.id], "Come at 7 instead of 8.",
            )

        self.assertEqual(result["queued"], 1)
        async_email.assert_not_called()
        self.assertFalse(Message.objects.exists())

        # A direct row: no announcement, the body on the row itself.
        delivery = AnnouncementDelivery.objects.get()
        self.assertIsNone(delivery.announcement_id)
        self.assertEqual(delivery.channel, "dm")
        self.assertEqual(delivery.direct_body, "Come at 7 instead of 8.")
        self.assertEqual(delivery.created_by_member_id, self.member.id)

        call_command("dispatch_announcements", verbosity=0)

        # TEXT, not a card: "come at 7 instead of 8" gains nothing from one.
        message = Message.objects.get()
        self.assertEqual(message.message_type, Message.Type.TEXT)
        self.assertIsNone(message.shared_recruitment_id)
        self.assertEqual(message.content, "Come at 7 instead of 8.")

    # -- 5. the reminder, once ------------------------------------

    def test_the_reminder_goes_out_once_for_a_trial_tomorrow(self):
        tomorrow = date(2030, 9, 14)
        evening = datetime(2030, 9, 13, 19, 0, tzinfo=IST)

        application = self._application(
            self._trial(dates=(tomorrow,), title="Tomorrow"),
            self._user("rem_player", "Rem"),
            status=RecruitmentApplication.Status.TRIAL_CONFIRMED,
        )

        with patch(
            "apps.recruitments.management.commands."
            "send_trial_reminders.send_trial_reminder_email",
            return_value=True,
        ) as email:
            call_command(
                "send_trial_reminders", now=evening.isoformat(), verbosity=0
            )

        self.assertEqual(email.call_count, 1)
        application.refresh_from_db()
        self.assertIsNotNone(application.trial_reminder_sent_at)
        stamped_at = application.trial_reminder_sent_at

        # A SECOND RUN IS A NO-OP — a stamped row is not selected again.
        with patch(
            "apps.recruitments.management.commands."
            "send_trial_reminders.send_trial_reminder_email",
            return_value=True,
        ) as second:
            call_command(
                "send_trial_reminders", now=evening.isoformat(), verbosity=0
            )

        second.assert_not_called()
        application.refresh_from_db()
        self.assertEqual(application.trial_reminder_sent_at, stamped_at)

        # ...and before 18:00 it does not act at all.
        application.trial_reminder_sent_at = None
        application.save(update_fields=["trial_reminder_sent_at"])
        with patch(
            "apps.recruitments.management.commands."
            "send_trial_reminders.send_trial_reminder_email",
            return_value=True,
        ) as too_early:
            call_command(
                "send_trial_reminders",
                now=datetime(2030, 9, 13, 9, 0, tzinfo=IST).isoformat(),
                verbosity=0,
            )
        too_early.assert_not_called()

    # -- 6. moving the date voids the reminder --------------------

    def test_moving_a_session_clears_the_reminder_stamp(self):
        trial = self._trial(dates=(date(2030, 9, 20),), title="Movable")
        session = trial.sessions.get()
        application = self._application(
            trial, self._user("move_player", "Move"),
            status=RecruitmentApplication.Status.TRIAL_CONFIRMED,
            trial_reminder_sent_at=timezone.now(),
        )

        # Same rows, same ids — only the DATE moves.
        RecruitmentService._sync_trial_sessions(trial, [
            {"id": session.id, "date": date(2030, 9, 21), "display_order": 0},
        ])

        application.refresh_from_db()
        self.assertIsNone(application.trial_reminder_sent_at)

        # A venue-only edit does NOT void it: the player still turns up
        # tomorrow, and re-reminding them would be noise.
        application.trial_reminder_sent_at = timezone.now()
        application.save(update_fields=["trial_reminder_sent_at"])
        changes = RecruitmentService._sync_trial_sessions(trial, [
            {
                "id": session.id, "date": date(2030, 9, 21),
                "venue_name": "New Ground", "display_order": 0,
            },
        ])
        application.refresh_from_db()
        self.assertIsNotNone(application.trial_reminder_sent_at)
        self.assertIn("session_venue", changes)

    # -- 7. the pass is personal ----------------------------------

    def test_the_pass_is_404_for_anybody_but_the_applicant(self):
        self.application.status = (
            RecruitmentApplication.Status.TRIAL_CONFIRMED
        )
        self.application.save(update_fields=["status"])
        # Confirmation is what mints the code; this fixture writes the status
        # directly, so it mints through the same helper the status path uses.
        mint_for(self.application)

        url = f"/recruitments/applications/{self.application.id}/pass"

        # The applicant reads their own.
        self.client.force_authenticate(user=self.player)
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertEqual(
            resp.data["data"]["application_id"], str(self.application.id)
        )
        # The booking reference, always present on a confirmed application.
        self.assertEqual(
            resp.data["data"]["pass_code"], self.application.pass_code
        )

        # Anybody else gets 404 — never 403, which would confirm the id.
        self.client.force_authenticate(user=self._user("nosy", "Nosy"))
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)


# =====================================================================
# THE PASS CODE - the booking reference
# =====================================================================


class PassCodeTests(APITestCase):
    """
    The code on a confirmed player's pass.

    It is a BOOKING REFERENCE, not a check-in code: nothing scans it and
    nothing checks anybody in against it. It is minted once, at confirmation,
    and never re-issued - a player may already have screenshotted it.
    """

    # THE TRIAL IS TODAY, which is the only day both halves of this read:
    # confirming players needs a trial that has not ended, and results open
    # on the trial day. The clock is pinned to it rather than chosen relative
    # to the real date, so these tests read the same in a year.
    TRIAL_DATE = date(2030, 3, 14)
    TRIAL_DAY_MORNING = datetime(2030, 3, 14, 10, 0, tzinfo=IST)

    def setUp(self):
        clock = patch(
            "django.utils.timezone.now", return_value=self.TRIAL_DAY_MORNING
        )
        clock.start()
        self.addCleanup(clock.stop)

        self.owner = self._user("pass_owner", "Owner")
        self.org = Organization.objects.create(
            name="Pass FC", username="passfc", type=Organization.Type.CLUB,
        )
        self.member = OrganizationMember.objects.create(
            organization=self.org, user=self.owner,
            role=OrganizationMember.Role.OWNER,
        )
        self.sport = Sport.objects.create(name="Boxing", icon_name="mdi:boxing")

        self.org_actor = Actor(
            actor_type="organization",
            organization=self.org,
            organization_member=self.member,
        )

        self.trial = self._trial()

    # -- factories ------------------------------------------------

    def _user(self, username, name):
        user = User.objects.create_user(
            email=f"{username}@example.com", password="pass1234",
            username=username,
        )
        accept_current_terms(user)
        UserProfile.objects.create(user=user, name=name)
        return user

    def _trial(self, **overrides):
        data = dict(
            organization=self.org, sport=self.sport, title="Pass Trial",
            recruitment_type="open_trial",
            status=Recruitment.Status.ACTIVE,
            visibility=Recruitment.Visibility.PUBLIC,
        )
        data.update(overrides)
        recruitment = Recruitment.objects.create(**data)
        TrialSession.objects.create(
            recruitment=recruitment, date=self.TRIAL_DATE
        )
        RecruitmentService._sync_trial_window(recruitment)
        recruitment.refresh_from_db()
        return recruitment

    def _application(self, recruitment, username, **overrides):
        data = dict(
            recruitment=recruitment, applicant=self._user(username, username),
            shared_name=username, shared_phone="9999999999",
        )
        data.update(overrides)
        return RecruitmentApplication.objects.create(**data)

    def _confirm(self, application):
        """Through the real status path, so the code is minted the real way."""
        ApplicationService.change_status(
            actor=self.org_actor,
            recruitment=application.recruitment,
            application_ids=[application.id],
            to_status=RecruitmentApplication.Status.TRIAL_CONFIRMED,
        )
        application.refresh_from_db()
        return application

    # -- 1. the code is minted once, and kept ---------------------

    def test_a_code_is_minted_on_confirm_and_survives_a_round_trip(self):
        application = self._application(self.trial, "code_player")
        self.assertEqual(application.pass_code, "")

        self._confirm(application)
        code = application.pass_code

        self.assertTrue(code)
        # XXXX-XXXX, from the confusion-free alphabet: no 0/O, no 1/I/L.
        # It is read aloud and written down by a person, which is why the
        # alphabet still matters with nobody scanning it.
        self.assertRegex(code, r"^[2-9A-HJ-NP-Z]{4}-[2-9A-HJ-NP-Z]{4}$")

        # OUT of confirmed and back must NOT re-issue: the player may already
        # have screenshotted the first one.
        ApplicationService.change_status(
            actor=self.org_actor, recruitment=self.trial,
            application_ids=[application.id],
            to_status=RecruitmentApplication.Status.SHORTLISTED,
        )
        self._confirm(application)

        self.assertEqual(application.pass_code, code)

    # -- 2. a code belongs to ONE application ---------------------

    def test_two_confirmations_on_one_trial_get_different_codes(self):
        first = self._confirm(self._application(self.trial, "first_player"))
        second = self._confirm(self._application(self.trial, "second_player"))

        self.assertTrue(first.pass_code)
        self.assertNotEqual(first.pass_code, second.pass_code)

    # -- 3. any confirmed applicant can be selected ---------------

    def test_any_confirmed_applicant_can_be_selected(self):
        """
        The app does not know who turned up - the org does. Nothing about a
        player's day at the ground gates the selection, so a whole batch of
        confirmed applicants goes through with no skips.
        """
        one = self._confirm(self._application(self.trial, "sel_one"))
        two = self._confirm(self._application(self.trial, "sel_two"))

        result = ApplicationService.change_status(
            actor=self.org_actor, recruitment=self.trial,
            application_ids=[one.id, two.id],
            to_status=RecruitmentApplication.Status.SELECTED,
        )

        self.assertEqual(result["updated"], [str(one.id), str(two.id)])
        self.assertEqual(result["skipped"], [])

        # ...and their codes survive the move: the pass is still theirs.
        one.refresh_from_db()
        self.assertTrue(one.pass_code)


# =====================================================================
# LEGACY STATUS VALUES - what a stale client still sends
# =====================================================================


class LegacyStatusMappingTests(APITestCase):
    """
    Goatza is an installed PWA: old JavaScript lives on phones for days after
    a deploy, and it still POSTs `invited` and `rejected`. The server accepts
    those words and translates them, because the alternative is a 400 and an
    org's decision silently lost.

    `rejected` is the interesting one — it splits on WHEN the org decided.
    Before the trial nobody ever saw the player (not_shortlisted); on or after
    it they watched them play (not_selected). Getting that backwards tells a
    player they were rejected on the day when nobody ever saw them.
    """

    TRIAL_DATE = date(2031, 4, 18)
    # Two clocks either side of the trial day, both pinned.
    BEFORE = datetime(2031, 4, 15, 11, 0, tzinfo=IST)
    ON_THE_DAY = datetime(2031, 4, 18, 11, 0, tzinfo=IST)

    def setUp(self):
        self.owner = self._user("legacy_owner", "Owner")
        self.org = Organization.objects.create(
            name="Legacy FC", username="legacyfc",
            type=Organization.Type.CLUB,
        )
        self.member = OrganizationMember.objects.create(
            organization=self.org, user=self.owner,
            role=OrganizationMember.Role.OWNER,
        )
        self.sport = Sport.objects.create(name="Kabaddi", icon_name="mdi:run")
        self.org_actor = Actor(
            actor_type="organization",
            organization=self.org,
            organization_member=self.member,
        )

    def _user(self, username, name):
        user = User.objects.create_user(
            email=f"{username}@example.com", password="pass1234",
            username=username,
        )
        accept_current_terms(user)
        UserProfile.objects.create(user=user, name=name)
        return user

    def _trial(self, **overrides):
        data = dict(
            organization=self.org, sport=self.sport, title="Legacy Trial",
            recruitment_type="open_trial",
            status=Recruitment.Status.ACTIVE,
            visibility=Recruitment.Visibility.PUBLIC,
        )
        data.update(overrides)
        recruitment = Recruitment.objects.create(**data)
        TrialSession.objects.create(
            recruitment=recruitment, date=self.TRIAL_DATE
        )
        RecruitmentService._sync_trial_window(recruitment)
        recruitment.refresh_from_db()
        return recruitment

    def _application(self, recruitment, username):
        return RecruitmentApplication.objects.create(
            recruitment=recruitment, applicant=self._user(username, username),
            shared_name=username, shared_phone="9999999999",
        )

    def _send_rejected(self, recruitment, application, now):
        """What a phone that has not reloaded since the v3 deploy sends."""
        with patch("django.utils.timezone.now", return_value=now):
            return ApplicationService.change_status(
                actor=self.org_actor,
                recruitment=recruitment,
                application_ids=[application.id],
                to_status="rejected",
            )

    # -- 1. before the trial: nobody ever saw them ----------------

    def test_rejected_before_the_trial_lands_as_not_shortlisted(self):
        trial = self._trial()
        application = self._application(trial, "legacy_early")

        result = self._send_rejected(trial, application, self.BEFORE)

        # ACCEPTED, not refused — the whole point of the grace release.
        self.assertEqual(result["updated"], [str(application.id)])
        self.assertEqual(result["skipped"], [])

        application.refresh_from_db()
        self.assertEqual(
            application.status,
            RecruitmentApplication.Status.NOT_SHORTLISTED,
        )

        # THE AUDIT TRAIL RECORDS WHAT HAPPENED, not the word that was sent.
        history = application.status_history.order_by("-created_at").first()
        self.assertEqual(
            history.to_status,
            RecruitmentApplication.Status.NOT_SHORTLISTED,
        )

    # -- 2. on the day: they came and did not make it -------------

    def test_rejected_on_or_after_the_trial_lands_as_not_selected(self):
        trial = self._trial()
        application = self._application(trial, "legacy_late")

        # ON the trial day, not after it: a decision made on the day itself is
        # a real result, which is the boundary every date rule here uses.
        result = self._send_rejected(trial, application, self.ON_THE_DAY)

        self.assertEqual(result["updated"], [str(application.id)])
        application.refresh_from_db()
        self.assertEqual(
            application.status, RecruitmentApplication.Status.NOT_SELECTED
        )

        # ...and a posting with no trial day at all is never "before" one.
        looking = Recruitment.objects.create(
            organization=self.org, sport=self.sport, title="Looking",
            recruitment_type="player_looking",
            status=Recruitment.Status.ACTIVE,
        )
        other = self._application(looking, "legacy_looking")
        self._send_rejected(looking, other, self.BEFORE)

        other.refresh_from_db()
        self.assertEqual(
            other.status, RecruitmentApplication.Status.NOT_SELECTED
        )

        # `invited` has no date question — it is always trial_confirmed.
        invited = self._application(trial, "legacy_invited")
        with patch("django.utils.timezone.now", return_value=self.BEFORE):
            ApplicationService.change_status(
                actor=self.org_actor, recruitment=trial,
                application_ids=[invited.id], to_status="invited",
            )
        invited.refresh_from_db()
        self.assertEqual(
            invited.status, RecruitmentApplication.Status.TRIAL_CONFIRMED
        )


# =====================================================================
# TRIAL FEEDBACK - the player's own account of how it went
# =====================================================================


class TrialFeedbackTests(APITestCase):
    """
    The player is asked too, because the org will not reliably come back.

    A HINT, NEVER THE TRUTH. Every test here is ultimately about one line:
    nothing a player submits may move `application.status`. The org's decision
    stays the org's, and the self-report only tells them where to look.
    """

    TRIAL_DATE = date(2030, 5, 10)
    # Two pinned clocks either side of the trial's last day. The rule is the
    # CALENDAR day in IST, so "the trial ran this morning" is still too early.
    DAY_BEFORE = datetime(2030, 5, 9, 10, 0, tzinfo=IST)
    DAY_AFTER = datetime(2030, 5, 11, 10, 0, tzinfo=IST)

    def setUp(self):
        self.owner = self._user("fb_owner", "Owner")
        self.org = Organization.objects.create(
            name="Feedback FC", username="feedbackfc",
            type=Organization.Type.CLUB,
        )
        OrganizationMember.objects.create(
            organization=self.org, user=self.owner,
            role=OrganizationMember.Role.OWNER,
        )
        self.sport = Sport.objects.create(name="Rugby", icon_name="mdi:rugby")
        self.trial = self._trial()

    # -- factories ------------------------------------------------

    def _user(self, username, name):
        user = User.objects.create_user(
            email=f"{username}@example.com", password="pass1234",
            username=username,
        )
        accept_current_terms(user)
        UserProfile.objects.create(user=user, name=name)
        return user

    def _trial(self):
        recruitment = Recruitment.objects.create(
            organization=self.org, sport=self.sport, title="Feedback Trial",
            recruitment_type="open_trial",
            status=Recruitment.Status.ACTIVE,
            visibility=Recruitment.Visibility.PUBLIC,
        )
        TrialSession.objects.create(
            recruitment=recruitment, date=self.TRIAL_DATE
        )
        RecruitmentService._sync_trial_window(recruitment)
        recruitment.refresh_from_db()
        return recruitment

    def _application(self, username, status_value):
        return RecruitmentApplication.objects.create(
            recruitment=self.trial,
            applicant=self._user(username, username),
            shared_name=username, shared_phone="9999999999",
            status=status_value,
        )

    def _post(self, application, body, when=None):
        self.client.force_authenticate(user=application.applicant)
        url = f"/recruitments/applications/{application.id}/feedback"
        with patch(
            "django.utils.timezone.now", return_value=when or self.DAY_AFTER
        ):
            return self.client.post(url, body, format="json")

    # -- 1. the happy path ----------------------------------------

    def test_a_confirmed_applicant_can_answer_once_the_trial_has_ended(self):
        application = self._application(
            "fb_came", RecruitmentApplication.Status.TRIAL_CONFIRMED
        )

        resp = self._post(application, {
            "attended": True,
            "outcome": "selected",
            "rating": 5,
            "feedback": "  Well run, good pitch.  ",
        })

        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)

        application.refresh_from_db()
        self.assertTrue(application.attended_self_reported)
        self.assertEqual(application.outcome_self_reported, "selected")
        self.assertEqual(application.trial_rating, 5)
        self.assertEqual(application.trial_feedback, "Well run, good pitch.")
        self.assertIsNotNone(application.feedback_at)

    # -- 2. too early is a 400, not a 404 -------------------------

    def test_answering_before_the_trial_ends_is_refused_with_400(self):
        """
        The application IS theirs; only the timing is wrong. A 404 here would
        read as "your application vanished" the day before a trial.
        """
        application = self._application(
            "fb_early", RecruitmentApplication.Status.TRIAL_CONFIRMED
        )

        resp = self._post(
            application,
            {"attended": True, "outcome": "waiting", "rating": 4},
            when=self.DAY_BEFORE,
        )

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

        application.refresh_from_db()
        self.assertIsNone(application.feedback_at)

    # -- 3. never called in, never asked --------------------------

    def test_a_not_shortlisted_applicant_is_refused_with_404(self):
        """
        Asking somebody who was never called to the trial how the trial went
        is a bad question. 404, not 403 - the refusal leaks nothing either.
        """
        application = self._application(
            "fb_never", RecruitmentApplication.Status.NOT_SHORTLISTED
        )

        resp = self._post(
            application, {"attended": True, "outcome": "waiting", "rating": 3}
        )

        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

        application.refresh_from_db()
        self.assertIsNone(application.feedback_at)

    # -- 4. did not attend is an ANSWER, not an error -------------

    def test_not_attending_clears_the_outcome_and_the_rating(self):
        application = self._application(
            "fb_absent", RecruitmentApplication.Status.TRIAL_CONFIRMED
        )

        # Whatever the client left in the form rides along and is dropped:
        # rating a trial you did not attend is meaningless, and so is an
        # outcome.
        resp = self._post(application, {
            "attended": False,
            "outcome": "selected",
            "rating": 5,
            "feedback": "Could not make it, work.",
        })

        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)

        application.refresh_from_db()
        self.assertFalse(application.attended_self_reported)
        self.assertEqual(application.outcome_self_reported, "")
        self.assertIsNone(application.trial_rating)
        # The note survives - "could not make it, work" is worth reading.
        self.assertEqual(
            application.trial_feedback, "Could not make it, work."
        )
        # Stamped, so "did not attend" is distinguishable from "never
        # answered".
        self.assertIsNotNone(application.feedback_at)

    # -- 5. answering again updates in place ----------------------

    def test_answering_twice_updates_in_place_and_re_stamps(self):
        """
        "Still waiting to hear" stops being true the week the org calls.
        """
        application = self._application(
            "fb_again", RecruitmentApplication.Status.TRIAL_CONFIRMED
        )

        self._post(
            application, {"attended": True, "outcome": "waiting", "rating": 4}
        )
        application.refresh_from_db()
        first_stamp = application.feedback_at
        self.assertEqual(application.outcome_self_reported, "waiting")

        later = datetime(2030, 6, 1, 10, 0, tzinfo=IST)
        resp = self._post(
            application,
            {"attended": True, "outcome": "selected", "rating": 5},
            when=later,
        )

        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)

        # ONE row, rewritten - not a second answer sitting beside a stale one.
        self.assertEqual(
            RecruitmentApplication.objects.filter(
                recruitment=self.trial
            ).count(),
            1,
        )
        application.refresh_from_db()
        self.assertEqual(application.outcome_self_reported, "selected")
        self.assertEqual(application.trial_rating, 5)
        self.assertGreater(application.feedback_at, first_stamp)

    # -- 6. THE WHOLE DESIGN --------------------------------------

    def test_answering_never_changes_the_application_status(self):
        """
        A player saying "I was selected" does not select them. If this ever
        fails, the separation the whole feature rests on is gone.
        """
        application = self._application(
            "fb_status", RecruitmentApplication.Status.TRIAL_CONFIRMED
        )

        self._post(
            application, {"attended": True, "outcome": "selected", "rating": 5}
        )

        application.refresh_from_db()
        self.assertEqual(
            application.status,
            RecruitmentApplication.Status.TRIAL_CONFIRMED,
        )
        # ...and no audit row either: that trail answers "who changed the
        # status", and this was not a status change.
        self.assertFalse(
            RecruitmentApplicationStatusHistory.objects.filter(
                application=application
            ).exists()
        )
