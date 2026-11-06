"""
A recruitment is shared only while it is live, and a card already sent outlives it.

Two questions with different answers on purpose: a closed trial can no longer be
forwarded, but the card somebody sent last week still has to render, and say so.
"""

from datetime import timedelta

from django.utils import timezone
from rest_framework import status

from apps.messaging.models import Conversation, Message
from apps.messaging.selectors.share_selectors import (
    ShareViewer,
    can_share_recruitment,
    can_view_shared_recruitment,
)
from apps.messaging.services.message_service import MessageService
from apps.messaging.tests import MESSAGES_URL, SHARE_URL, MessagingShareTestCase
from apps.recruitments.models import Recruitment
from apps.recruitments.serializers.recruitment_list_serializers import (
    RecruitmentListSerializer,
)
from apps.usernames.services.username_service import UsernameService
from core.actor import Actor

PAST = timedelta(days=2)


class LiveStates:
    """Every way a recruitment stops being live, as the column it changes."""

    NOT_LIVE = {
        "draft": {"status": Recruitment.Status.DRAFT},
        "cancelled": {"status": Recruitment.Status.CANCELLED},
        "closed": {"status": Recruitment.Status.CLOSED},
        "deleted": {"is_deleted": True},
    }

    def _make(self, **fields):
        Recruitment.objects.filter(id=self.recruitment.id).update(**fields)
        self.recruitment.refresh_from_db()

    def _past_deadline(self):
        self._make(application_deadline=timezone.now() - PAST)

    def _trial_over(self):
        # The deadline first: the database holds it to on or before the trial.
        self._make(
            application_deadline=timezone.now() - PAST - timedelta(days=1),
            event_date=timezone.now() - PAST,
            trial_end_date=timezone.now() - PAST,
        )

    def _full(self):
        self._make(max_applications=1, applications_count=1)


class NewShareTests(LiveStates, MessagingShareTestCase):
    def _share(self, **targets):
        return self.client.post(
            SHARE_URL,
            self._share_body("recruitment", self.recruitment.id, **targets),
            format="json",
        )

    def _assert_not_shareable(self):
        conversation = self._mutual_conversation()

        response = self._share(conversation_ids=[str(conversation.id)])

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["data"]["sent"], [])
        self.assertEqual(
            response.data["data"]["failed"],
            [{"id": str(conversation.id), "reason": "not_shareable"}],
        )
        self.assertFalse(Message.objects.filter(conversation=conversation).exists())

    def test_active_and_accepting_is_sent(self):
        conversation = self._mutual_conversation()

        response = self._share(conversation_ids=[str(conversation.id)])

        self.assertEqual(response.data["data"]["sent"], [str(conversation.id)])
        self.assertEqual(response.data["data"]["failed"], [])

    def test_each_status_that_is_not_live(self):
        for name, fields in self.NOT_LIVE.items():
            with self.subTest(name):
                self._make(status=Recruitment.Status.ACTIVE, is_deleted=False)
                self._make(**fields)
                self._assert_not_shareable()

    def test_trial_over(self):
        self._trial_over()
        self._assert_not_shareable()

    def test_deadline_passed(self):
        self._past_deadline()
        self._assert_not_shareable()

    def test_applications_full(self):
        self._full()
        self._assert_not_shareable()

    def test_a_recipient_with_no_thread_fails_the_same_and_gets_no_thread(self):
        self._make(status=Recruitment.Status.CLOSED)
        before = Conversation.objects.count()

        response = self._share(
            recipients=[{"actor_type": "user", "actor_id": str(self.receiver.id)}]
        )

        self.assertEqual(
            response.data["data"]["failed"],
            [{"id": str(self.receiver.id), "reason": "not_shareable"}],
        )
        self.assertEqual(Conversation.objects.count(), before)

    def test_one_the_sender_cannot_see_is_the_same_404_as_no_such_id(self):
        conversation = self._mutual_conversation()
        missing = self.client.post(
            SHARE_URL,
            self._share_body(
                "recruitment",
                "00000000-0000-0000-0000-000000000000",
                conversation_ids=[str(conversation.id)],
            ),
            format="json",
        )

        for visibility in (
            Recruitment.Visibility.PRIVATE,
            Recruitment.Visibility.FOLLOWERS_ONLY,
        ):
            with self.subTest(visibility):
                self._make(visibility=visibility)

                hidden = self._share(conversation_ids=[str(conversation.id)])

                self.assertEqual(hidden.status_code, status.HTTP_404_NOT_FOUND)
                self.assertEqual(hidden.status_code, missing.status_code)
                self.assertEqual(hidden.data["data"], missing.data["data"])

    def test_an_orgs_own_update_to_its_applicants_still_works_once_closed(self):
        self._make(status=Recruitment.Status.CLOSED)
        conversation = self._conversation(
            actor_org=self.other_org, target_user=self.receiver
        )

        message = MessageService.send_shared_recruitment(
            conversation,
            sender_org=self.other_org,
            recruitment=self.recruitment,
            note="Venue moved",
        )

        self.assertEqual(message.shared_recruitment_id, self.recruitment.id)


class PreviewOfASentCardTests(LiveStates, MessagingShareTestCase):
    def setUp(self):
        super().setUp()
        self.conversation = self._mutual_conversation()
        self.client.post(
            SHARE_URL,
            self._share_body(
                "recruitment",
                self.recruitment.id,
                conversation_ids=[str(self.conversation.id)],
            ),
            format="json",
        )

    def _preview(self):
        response = self.client.get(
            MESSAGES_URL, {"conversation_id": str(self.conversation.id)}
        )
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]["results"][0]["shared_recruitment_preview"]

    def test_live_carries_the_flags(self):
        preview = self._preview()

        self.assertFalse(preview["unavailable"])
        self.assertIs(preview["is_trial_over"], False)
        self.assertIs(preview["is_accepting_applications"], True)

    def test_closed_is_still_shown_and_says_so(self):
        self._make(status=Recruitment.Status.CLOSED)

        preview = self._preview()

        self.assertFalse(preview["unavailable"])
        self.assertEqual(preview["status"], "closed")
        self.assertIs(preview["is_accepting_applications"], False)

    def test_a_trial_that_ended_is_still_shown_and_says_so(self):
        self._trial_over()

        preview = self._preview()

        self.assertFalse(preview["unavailable"])
        self.assertIs(preview["is_trial_over"], True)

    def test_draft_cancelled_and_deleted_are_unavailable(self):
        for name in ("draft", "cancelled", "deleted"):
            with self.subTest(name):
                self._make(status=Recruitment.Status.ACTIVE, is_deleted=False)
                self._make(**self.NOT_LIVE[name])

                self.assertEqual(self._preview(), {"unavailable": True})


class TheTwoRulesTests(LiveStates, MessagingShareTestCase):
    def test_closed_can_be_seen_but_not_shared(self):
        self._make(status=Recruitment.Status.CLOSED)
        viewer = ShareViewer(user=self.sender)

        self.assertTrue(can_view_shared_recruitment(self.recruitment, viewer))
        self.assertFalse(can_share_recruitment(self.recruitment, viewer))

    def test_neither_for_nothing(self):
        viewer = ShareViewer(user=self.sender)

        self.assertFalse(can_view_shared_recruitment(None, viewer))
        self.assertFalse(can_share_recruitment(None, viewer))


class CanShareOnListAndDetailTests(LiveStates, MessagingShareTestCase):
    def setUp(self):
        super().setUp()
        # The org-scoped list resolves a username through the shared namespace.
        UsernameService.claim(self.other_org.username, organization=self.other_org)

    def _from_list(self):
        response = self.client.get(
            "/recruitments/list", {"username": self.other_org.username}
        )
        self.assertEqual(response.status_code, 200, response.data)
        rows = [
            row for row in response.data["data"]["results"]
            if row["id"] == str(self.recruitment.id)
        ]
        return rows[0]["can_share"]

    def _from_detail(self):
        response = self.client.get(f"/recruitments/{self.recruitment.id}/details")
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]["can_share"]

    def test_live_is_true_on_both(self):
        self.assertIs(self._from_list(), True)
        self.assertIs(self._from_detail(), True)

    def test_closed_is_false_on_both(self):
        self._make(status=Recruitment.Status.CLOSED)

        # A closed one drops out of other people's lists, so the card that matters
        # is the one a list serializer renders for somebody still holding it.
        card = RecruitmentListSerializer(
            self.recruitment, context={"actor": Actor("user", user=self.sender)}
        ).data

        self.assertIs(card["can_share"], False)
        self.assertIs(self._from_detail(), False)

    def test_deadline_passed_is_false_on_both(self):
        self._past_deadline()

        self.assertIs(self._from_list(), False)
        self.assertIs(self._from_detail(), False)

    def test_the_ranked_player_list_carries_it_too(self):
        response = self.client.get("/recruitments/list")

        self.assertEqual(response.status_code, 200, response.data)
        for row in response.data["data"]["results"]:
            self.assertIn("can_share", row)
            self.assertIsInstance(row["can_share"], bool)

    def test_a_logged_out_viewer_of_a_public_live_trial_may_share_the_link(self):
        self.client.force_authenticate(user=None)

        response = self.client.get(f"/public/recruitments/{self.recruitment.id}")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertIs(response.data["data"]["can_share"], True)

    def test_the_owner_sees_false_on_their_own_draft(self):
        self._make(status=Recruitment.Status.DRAFT)
        viewer = ShareViewer(org=self.other_org)

        self.assertFalse(can_share_recruitment(self.recruitment, viewer))
