import uuid
from decimal import Decimal
from datetime import timedelta
from unittest.mock import patch

from django.conf import settings
from django.core.cache import cache
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
