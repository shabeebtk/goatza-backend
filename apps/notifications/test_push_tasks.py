"""
The push now leaves in a worker, and the in-app row no longer depends on it.

THE SPLIT IS THE POINT. ``NotificationService`` writes the row inside the
request; ``notifications.push`` delivers it afterwards. So FCM being slow, the
broker being down or the worker being dead costs the push and never the
notification — the badge is late, the row is already in Postgres.

Two properties are worth pinning down and both are about failure. A
notification whose row is gone by the time the worker reaches it must return
quietly (deleted, or a rolled-back transaction) — that is routine, not an
error. And a fan-out to several recipients must not be abandoned because the
first device is unreachable: an org notification goes to every OWNER/ADMIN, and
one stale token used to be enough to silence the rest of the club.
"""

import uuid
from unittest.mock import patch

from django.test import TestCase, override_settings

from apps.accounts.models import User, UserProfile
from apps.legal.testing import accept_current_terms
from apps.notifications.models import Notification
from apps.notifications.tasks import push_notification
from apps.organization.models import Organization, OrganizationMember
from apps.posts.models import Post

FCM = "apps.notifications.tasks.FCMService.send_to_user"


@override_settings(
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"]
)
class PushNotificationTaskTests(TestCase):
    """One player, one club with two admins, and a post to notify about."""

    def setUp(self):
        self.actor = self._user("alice")
        self.recipient = self._user("bob")

        self.club = Organization.objects.create(
            name="Dream FC", username="dreamfc", type=Organization.Type.CLUB,
        )
        self.owner = self._user("owner")
        self.admin = self._user("admin")
        for user, role in (
            (self.owner, OrganizationMember.Role.OWNER),
            (self.admin, OrganizationMember.Role.ADMIN),
        ):
            OrganizationMember.objects.create(
                organization=self.club, user=user, role=role
            )

        self.post = Post.objects.create(author_user=self.recipient, content="hi")

    def _user(self, username):
        user = User.objects.create_user(
            email=f"{username}@example.com", password="pass1234", username=username,
        )
        accept_current_terms(user)
        UserProfile.objects.create(user=user, name=username.title())
        return user

    def _notification(self, recipient_user=None, recipient_org=None):
        return Notification.objects.create(
            type=Notification.Type.LIKE,
            group_key=f"like:post:{self.post.id}",
            post=self.post,
            actor_user=self.actor,
            recipient_user=recipient_user,
            recipient_org=recipient_org,
        )

    # ── the row is gone ──────────────────────────────────────────

    def test_a_missing_notification_returns_quietly(self):
        # Deleted before the worker got to it, or its transaction rolled back
        # after the publish. Routine — INFO and return, no exception.
        with patch(FCM) as send:
            with self.assertLogs("apps.notifications.tasks", level="INFO") as logs:
                result = push_notification(str(uuid.uuid4()))

        self.assertIsNone(result)
        send.assert_not_called()
        self.assertIn("notification gone", logs.output[0])

    # ── fan-out ──────────────────────────────────────────────────

    def test_a_user_recipient_gets_one_push(self):
        notification = self._notification(recipient_user=self.recipient)

        with patch(FCM) as send:
            push_notification(str(notification.id))

        send.assert_called_once()
        pushed_to, payload = send.call_args.args
        self.assertEqual(pushed_to.id, self.recipient.id)
        self.assertEqual(payload["notification_id"], str(notification.id))

    def test_an_org_recipient_fans_out_to_every_admin(self):
        notification = self._notification(recipient_org=self.club)

        with patch(FCM) as send:
            push_notification(str(notification.id))

        self.assertEqual(send.call_count, 2)
        self.assertEqual(
            {call.args[0].id for call in send.call_args_list},
            {self.owner.id, self.admin.id},
        )

    def test_one_unreachable_recipient_does_not_cost_the_others_their_push(self):
        """
        THE REASON THE TRY/EXCEPT IS PER RECIPIENT. Without it, one stale token
        on the first admin's phone would stop the whole club being told —
        silently, since the request has already succeeded.
        """
        notification = self._notification(recipient_org=self.club)

        with patch(FCM, side_effect=[ConnectionError("token gone"), None]) as send:
            with self.assertLogs("apps.notifications.tasks", level="INFO") as logs:
                push_notification(str(notification.id))

        self.assertEqual(send.call_count, 2)       # it kept going

        warnings = [line for line in logs.output if line.startswith("WARNING")]
        self.assertEqual(len(warnings), 1)
        self.assertIn("recipient failed", warnings[0])

        done = [line for line in logs.output if "notifications.push | done" in line]
        self.assertEqual(len(done), 1)
        self.assertIn("tried=2 | sent=1 | failed=1", done[0])

    def test_every_recipient_failing_is_logged_at_error(self):
        # Not one bad device but a dead push channel, which deserves a Sentry
        # event rather than a WARNING nobody reads.
        notification = self._notification(recipient_user=self.recipient)

        with patch(FCM, side_effect=ConnectionError("fcm is gone")):
            with self.assertLogs("apps.notifications.tasks", level="ERROR") as logs:
                push_notification(str(notification.id))

        self.assertTrue(
            any("every recipient failed" in line for line in logs.output)
        )

    def test_a_recipient_is_required_by_the_database(self):
        # Worth recording rather than testing the empty fan-out: the
        # notification_recipient_user_or_org CHECK means a persisted row always
        # has somebody to notify, so _get_recipient_users can only return []
        # for an org whose admins have all left.
        from django.db import IntegrityError, transaction

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self._notification()   # neither user nor org

    def test_an_org_with_no_admins_left_sends_nothing(self):
        empty_club = Organization.objects.create(
            name="Ghost FC", username="ghostfc", type=Organization.Type.CLUB,
        )
        notification = self._notification(recipient_org=empty_club)

        with patch(FCM) as send:
            push_notification(str(notification.id))

        send.assert_not_called()

    def test_acks_late_is_off_so_a_crash_cannot_buzz_a_phone_twice(self):
        # No "pushed_at" column to make a second run a no-op, and the in-app
        # row is already safely stored — at most once is the right trade.
        self.assertFalse(push_notification.acks_late)


@override_settings(
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"]
)
class NotificationDispatchTests(TestCase):
    """
    ``NotificationService`` -> ``_dispatch`` -> ``enqueue``.

    The service's many public methods all funnel through one ``_dispatch``, so
    this asserts the funnel rather than every method: an id is handed over, not
    an instance, because the serializer is JSON and a model would not survive
    the trip.
    """

    def setUp(self):
        self.actor = User.objects.create_user(
            email="actor@example.com", password="pass1234", username="actor",
        )
        accept_current_terms(self.actor)
        UserProfile.objects.create(user=self.actor, name="Actor")

        self.target = User.objects.create_user(
            email="target@example.com", password="pass1234", username="target",
        )
        accept_current_terms(self.target)
        UserProfile.objects.create(user=self.target, name="Target")

    def test_create_dispatches_the_notification_id_through_enqueue(self):
        from apps.notifications.services.notification_service import (
            NotificationService,
        )

        with patch(
            "apps.notifications.services.notification_service.enqueue"
        ) as enqueue:
            NotificationService.follow(
                actor_user=self.actor, target_user=self.target
            )

        notification = Notification.objects.get(type=Notification.Type.FOLLOW)

        enqueue.assert_called_once()
        task, args = enqueue.call_args.args
        self.assertEqual(task.name, "notifications.push")
        # A STRING id, not the instance and not a UUID object.
        self.assertEqual(args, (str(notification.id),))
        self.assertIsInstance(args[0], str)

    def test_the_row_is_written_even_when_the_push_cannot_be_delivered(self):
        """
        The split that makes the whole design worth it: the in-app
        notification survives a dead FCM, a dead broker and a dead worker,
        because it was committed before any of them was consulted.
        """
        from apps.notifications.services.notification_service import (
            NotificationService,
        )

        with patch(FCM, side_effect=ConnectionError("fcm is gone")):
            with self.assertLogs("apps.notifications.tasks", level="ERROR"):
                with self.captureOnCommitCallbacks(execute=True):
                    NotificationService.follow(
                        actor_user=self.actor, target_user=self.target
                    )

        self.assertTrue(
            Notification.objects.filter(type=Notification.Type.FOLLOW).exists()
        )

    def test_nothing_is_dispatched_when_the_transaction_rolls_back(self):
        from django.db import transaction

        from apps.notifications.services.notification_service import (
            NotificationService,
        )

        with patch("apps.notifications.tasks.FCMService.send_to_user") as send:
            with self.captureOnCommitCallbacks(execute=True):
                with self.assertRaises(RuntimeError):
                    with transaction.atomic():
                        NotificationService.follow(
                            actor_user=self.actor, target_user=self.target
                        )
                        raise RuntimeError("request failed after the notify")

        send.assert_not_called()
        self.assertFalse(Notification.objects.exists())
