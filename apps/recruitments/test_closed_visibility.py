"""
A closed recruitment opens for non-owners, and that is all it does.

A shared card, a notification or a bookmark outlives the close, so the link has to
keep working and say so. Everything that used to lean on the 404 for a closed one
checks the status itself: these pin both halves.
"""

from rest_framework import status

from apps.accounts.models import User
from apps.connections.models import Follow
from apps.messaging.tests import MessagingShareTestCase
from apps.organization.models import OrganizationMember
from apps.recruitments.models import Recruitment, RecruitmentApplication
from apps.recruitments.selectors.recruitment_selectors import RecruitmentSelector
from core.actor import Actor

NOT_ACCEPTING = "This recruitment is not accepting applications."


class ClosedVisibilityBase(MessagingShareTestCase):
    def _make(self, **fields):
        Recruitment.objects.filter(id=self.recruitment.id).update(**fields)
        self.recruitment.refresh_from_db()

    def _detail(self):
        return self.client.get(f"/recruitments/{self.recruitment.id}/details")

    def _public(self):
        self.client.force_authenticate(user=None)
        return self.client.get(f"/public/recruitments/{self.recruitment.id}")


class WhoCanOpenItTests(ClosedVisibilityBase):
    def test_closed_public_opens_for_a_non_owner_and_says_it_is_closed(self):
        self._make(status=Recruitment.Status.CLOSED)

        response = self._detail()

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertIs(response.data["data"]["can_share"], False)
        self.assertIs(response.data["data"]["is_accepting_applications"], False)
        self.assertIs(response.data["data"]["can_apply"], False)

    def test_closed_public_opens_logged_out_too(self):
        self._make(status=Recruitment.Status.CLOSED)

        response = self._public()

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertIs(response.data["data"]["can_share"], False)
        self.assertIs(response.data["data"]["is_accepting_applications"], False)
        self.assertIs(response.data["data"]["can_apply"], False)

    def test_closed_followers_only_is_404_for_a_non_follower(self):
        self._make(
            status=Recruitment.Status.CLOSED,
            visibility=Recruitment.Visibility.FOLLOWERS_ONLY,
        )

        self.assertEqual(self._detail().status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(self._public().status_code, status.HTTP_404_NOT_FOUND)

    def test_closed_followers_only_opens_for_a_follower(self):
        self._make(
            status=Recruitment.Status.CLOSED,
            visibility=Recruitment.Visibility.FOLLOWERS_ONLY,
        )
        Follow.objects.create(follower_user=self.sender, following_org=self.other_org)

        response = self._detail()

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertIs(response.data["data"]["can_share"], False)

    def test_closed_private_is_404(self):
        self._make(
            status=Recruitment.Status.CLOSED,
            visibility=Recruitment.Visibility.PRIVATE,
        )

        self.assertEqual(self._detail().status_code, status.HTTP_404_NOT_FOUND)

    def test_draft_and_cancelled_are_404_for_non_owners(self):
        for state in (Recruitment.Status.DRAFT, Recruitment.Status.CANCELLED):
            with self.subTest(state):
                self._make(status=state)
                self.client.force_authenticate(user=self.sender)

                self.assertEqual(self._detail().status_code, status.HTTP_404_NOT_FOUND)
                self.assertEqual(self._public().status_code, status.HTTP_404_NOT_FOUND)

    def test_deleted_is_404_even_if_closed(self):
        self._make(status=Recruitment.Status.CLOSED, is_deleted=True)

        self.assertEqual(self._detail().status_code, status.HTTP_404_NOT_FOUND)

    def test_the_owner_still_opens_all_of_them(self):
        owner = Actor(actor_type="organization", organization=self.other_org)

        for state in (
            Recruitment.Status.DRAFT,
            Recruitment.Status.CANCELLED,
            Recruitment.Status.CLOSED,
        ):
            with self.subTest(state):
                self._make(status=state)

                self.assertIsNotNone(
                    RecruitmentSelector.get_recruitment_detail(
                        recruitment_id=self.recruitment.id, actor=owner
                    )
                )


class WhatItDoesNotUnlockTests(ClosedVisibilityBase):
    def setUp(self):
        super().setUp()
        self._make(status=Recruitment.Status.CLOSED)

    def test_applying_is_refused_with_the_existing_message(self):
        self.sender.role = User.Role.PLAYER
        self.sender.save(update_fields=["role"])

        response = self.client.post(
            f"/recruitments/{self.recruitment.id}/apply",
            {
                "shared_name": "Sender",
                "shared_phone": "+919876543210",
                "shared_email": "sender@example.com",
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, response.data)
        self.assertIn(NOT_ACCEPTING, str(response.data))
        self.assertFalse(RecruitmentApplication.objects.exists())

    def test_it_stays_out_of_other_peoples_lists(self):
        response = self.client.get("/recruitments/list")

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertNotIn(
            str(self.recruitment.id),
            [row["id"] for row in response.data["data"]["results"]],
        )

    def _as_a_rival_org(self):
        OrganizationMember.objects.get_or_create(
            organization=self.org,
            user=self.sender,
            defaults={"role": OrganizationMember.Role.OWNER},
        )
        return self._org_headers(self.org)

    def test_another_org_still_cannot_edit_it(self):
        response = self.client.patch(
            f"/recruitments/{self.recruitment.id}/update",
            {"title": "Mine now"},
            format="json",
            **self._as_a_rival_org(),
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN, response.data)
        self.recruitment.refresh_from_db()
        self.assertEqual(self.recruitment.title, "U17 Open Trials")

    def test_another_org_still_cannot_reopen_it(self):
        response = self.client.patch(
            f"/recruitments/{self.recruitment.id}/status",
            {"status": "active"},
            format="json",
            **self._as_a_rival_org(),
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN, response.data)
        self.recruitment.refresh_from_db()
        self.assertEqual(self.recruitment.status, Recruitment.Status.CLOSED)


class WhatItDoesOpenTests(ClosedVisibilityBase):
    def setUp(self):
        super().setUp()
        self._make(status=Recruitment.Status.CLOSED)

    def test_a_shortlist_entry_can_be_taken_off_after_it_closes(self):
        self._make(status=Recruitment.Status.ACTIVE)
        url = f"/recruitments/{self.recruitment.id}/save"
        self.assertIs(self.client.post(url).data["data"]["is_saved"], True)
        self._make(status=Recruitment.Status.CLOSED)

        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertIs(response.data["data"]["is_saved"], False)

    def test_its_announcements_can_still_be_read(self):
        response = self.client.get(f"/recruitments/{self.recruitment.id}/announcements")

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
