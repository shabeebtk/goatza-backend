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
from accounts.models import User
from organization.models import Organization, OrganizationMember
from connections.models import Follow
from sports.models import Sport, SportPosition
from recruitments.models import (
    Recruitment,
    RecruitmentMedia,
    RecruitmentPosition,
    RecruitmentApplication,
    RecruitmentApplicationAnswer,
    RecruitmentApplicationStatusHistory,
    RecruitmentQuestion,
    RecruitmentAgeCategory,
    RecruitmentEligibilityCriteria,
)
from recruitments.selectors.recruitment_selectors import RecruitmentSelector
from notifications.models import Notification

SIGNATURE_URL = "/user/get/upload/signature"
CREATE_URL = "/recruitments/create"

# Deterministic Cloudinary config so URL/signature validation is env-independent.
CLOUD = "democloud"


@override_settings(
    CLOUDINARY_CLOUD_NAME=CLOUD,
    CLOUDINARY_API_KEY="test-key",
    CLOUDINARY_API_SECRET="test-secret",
)
class RecruitmentMediaPipelineTests(APITestCase):

    def setUp(self):
        self.user = User.objects.create_user(
            email="owner@example.com",
            password="pass1234",
            username="owner",
        )
        self.other_user = User.objects.create_user(
            email="stranger@example.com",
            password="pass1234",
            username="stranger",
        )

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
            f"https://res.cloudinary.com/{CLOUD}/image/upload/"
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

    def test_create_accepts_long_cloudinary_url(self):
        # Real Cloudinary URLs (full version segment + deep recruitment folder
        # path) exceed the old 200-char URLField default. Regression for
        # "value too long for type character varying(200)".
        public_id = self._public_id()
        long_url = (
            f"https://res.cloudinary.com/{CLOUD}/image/upload/"
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
        self.assertEqual(data["provider"], "cloudinary")
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

    def test_create_rejects_non_cloudinary_url(self):
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

    # ── update: orphaned Cloudinary cleanup ──────────────────────

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
            "recruitments.services.recruitment_service.get_storage_service"
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
        self.org = Organization.objects.create(
            name="Kite FC",
            username="kitefc",
            type=Organization.Type.CLUB,
        )
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


class RecruitmentApplicationLifecycleTests(APITestCase):
    """Withdraw + reapply, org bulk/single status changes, and the player
    status-change notifications."""

    def setUp(self):
        cache.clear()
        self.owner = User.objects.create_user(
            email="own_l@example.com", password="pass1234", username="owner_l"
        )
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
        self.other = User.objects.create_user(
            email="p2_l@example.com", password="pass1234", username="player2_l"
        )
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
                {"application_ids": [str(a1.id), str(a2.id)], "status": "shortlisted"},
                format="json", **self._org_headers(),
            )

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(set(resp.data["data"]["updated"]), {str(a1.id), str(a2.id)})
        self.assertIn("status_counts", resp.data["data"])

        a1.refresh_from_db()
        self.assertEqual(a1.status, "shortlisted")
        self.assertEqual(a1.reviewed_by, self.member)
        self.assertIsNotNone(a1.reviewed_at)
        self.assertTrue(
            RecruitmentApplicationStatusHistory.objects.filter(
                application=a1, to_status="shortlisted", changed_by=self.member,
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
        a_nochange = self._make_app(third, "shortlisted")
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
                    "status": "shortlisted",
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
                {"status": "reviewing"}, format="json", **self._org_headers(),
            )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        a.refresh_from_db()
        self.assertEqual(a.status, "reviewing")
        self.assertEqual(a.reviewed_by, self.member)
        self.assertEqual(
            Notification.objects.filter(
                type="recruitment_application_status", recipient_user=self.player
            ).count(),
            1,
        )

    def test_single_status_invited_rejected(self):
        # `invited` is reserved for the future personal-invite feature — the org
        # status API must reject it as a target.
        a = self._make_app(self.player, "applied")
        self.client.force_authenticate(user=self.owner)
        resp = self.client.post(
            f"/recruitments/applications/{a.id}/status",
            {"status": "invited"}, format="json", **self._org_headers(),
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
        from notifications.services.notification_service import (
            build_notification_payload,
        )

        a = self._make_app(self.player, "applied")
        self.client.force_authenticate(user=self.owner)
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(
                f"/recruitments/applications/{a.id}/status",
                {"status": "shortlisted"}, format="json", **self._org_headers(),
            )

        notif = Notification.objects.get(
            type="recruitment_application_status", recipient_user=self.player
        )
        self.assertEqual(notif.recruitment_id, self.recruitment.id)
        self.assertEqual(str(notif.actor_org_id), str(self.org.id))
        self.assertEqual(notif.data["to_status"], "shortlisted")
        self.assertEqual(notif.data["application_id"], str(a.id))

        payload = build_notification_payload(notif)
        self.assertEqual(payload["type"], "recruitment_application_status")
        self.assertIn("shortlisted your application", payload["title"])
        self.assertIn("was shortlisted", payload["body"])
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
        self.owner = User.objects.create_user(
            email="disc_o@example.com", password="pass1234",
            username="disc_owner",
        )
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
        self.other_player = User.objects.create_user(
            email="elig_p2@example.com", password="pass1234",
            username="elig_player2",
        )
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
