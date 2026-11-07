"""
The review fixes to organization members. Each one closes a way for a person who
has lost their power, or never had it, to act anyway, or for the app to give away
something it promised not to.

Races are written two ways. Where the claim is about what a waiter sees, the wait
is simulated by doing the other request's write at the moment the lock is taken,
which is deterministic. The one claim about real concurrency runs on real threads.
"""

import logging
import threading
from datetime import timedelta
from unittest.mock import patch

from asgiref.sync import async_to_sync
from channels.testing import WebsocketCommunicator
from django.conf import settings
from django.contrib.admin.sites import AdminSite
from django.core.cache import cache
from django.db import connection
from django.test import RequestFactory, SimpleTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from rest_framework.test import APITransactionTestCase

from apps.accounts.services.account_deletion_service import (
    confirm_account_deletion,
    sole_owned_organizations,
)
from apps.messaging.consumers.chat_consumers import ChatConsumer
from apps.messaging.consumers.notification_consumers import UserNotificationsConsumer
from apps.messaging.models import Conversation, ConversationParticipant
from apps.messaging.throttles import canonical_actor_id
from apps.moderation.models import Block
from apps.notifications.models import Notification
from apps.organization.admin import (
    OrganizationMemberAdmin,
    OrganizationMemberInline,
)
from apps.organization.models import (
    Organization,
    OrganizationInvite,
    OrganizationMember,
)
from apps.organization.permissions import OrgPermission
from apps.organization.services.organization_member_service import (
    MANAGER_LOST_MESSAGE,
    OrganizationMemberService,
    _lock_members,
)
from apps.organization.tests import SERVICE, MembersTestBase, make_user
from apps.organization.throttles import OrgInviteCreateThrottle
from core.actor import Actor
from core.log_redaction import (
    RedactTokenPathsFilter,
    sentry_redact_breadcrumb,
    sentry_redact_event,
)
from utils.realtime import WS_CLOSE_ORG_ACCESS_REVOKED

OWNER = OrganizationMember.Role.OWNER
COACH = OrganizationMember.Role.COACH
STAFF = OrganizationMember.Role.STAFF

MANAGE_GRANT = {"grant": [OrgPermission.MEMBERS_MANAGE.value]}


class MemberFixesBase(MembersTestBase):
    def setUp(self):
        super().setUp()
        self.second_owner = make_user("secondowner", "Seth Second")
        self.second_owner_member = self._member(self.second_owner, OWNER)

    def _grant_manage(self, member):
        member.permission_overrides = MANAGE_GRANT
        member.save(update_fields=["permission_overrides"])

    def _announced(self):
        """The org-group half of every membership announcement sent so far."""
        return [
            call.args[1]
            for call in self.realtime.call_args_list
            if call.args[1].get("type") == "org_member_changed"
        ]


# ---------------------------------------------------------------------
# 0.1 Account deletion cannot orphan an org
# ---------------------------------------------------------------------


class AccountDeletionOwnerTests(MemberFixesBase):
    def test_a_working_co_owner_is_enough(self):
        self.assertEqual(sole_owned_organizations(self.owner), [])

    def test_a_deactivated_co_owner_does_not_count(self):
        self.second_owner.is_active = False
        self.second_owner.save(update_fields=["is_active"])

        self.assertEqual(sole_owned_organizations(self.owner), [self.org])

        with self.assertRaises(ValidationError):
            confirm_account_deletion(self.owner, password="pass1234")

    def test_a_co_owner_scheduled_for_deletion_does_not_count(self):
        self.second_owner.deletion_requested_at = timezone.now()
        self.second_owner.save(update_fields=["deletion_requested_at"])

        self.assertEqual(sole_owned_organizations(self.owner), [self.org])

        with self.assertRaises(ValidationError):
            confirm_account_deletion(self.owner, password="pass1234")

        self.owner.refresh_from_db()
        self.assertTrue(self.owner.is_active)

    def test_the_check_runs_after_the_lock_so_a_waiter_sees_the_co_owner_go(self):
        """The co-owner leaves while this deletion is queued behind them."""

        def co_owner_leaves_first(organization):
            OrganizationMember.objects.filter(id=self.second_owner_member.id).update(
                status=OrganizationMember.Status.LEFT
            )
            return _lock_members(organization)

        with patch(f"{SERVICE}._lock_members", side_effect=co_owner_leaves_first):
            with self.assertRaises(ValidationError):
                confirm_account_deletion(self.owner, password="pass1234")

    def test_a_successful_deletion_goes_through(self):
        confirm_account_deletion(self.owner, password="pass1234")

        self.owner.refresh_from_db()
        self.assertFalse(self.owner.is_active)


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ConcurrentLeaveAndDeleteTests(APITransactionTestCase):
    def test_a_leave_and_a_delete_at_once_cannot_leave_zero_owners(self):
        cache.clear()
        leaver = make_user("leaver")
        deleter = make_user("deleter")
        org = Organization.objects.create(
            name="Dream FC", username="dreamfc", type=Organization.Type.CLUB
        )
        leaver_member = OrganizationMember.objects.create(
            organization=org, user=leaver, role=OWNER
        )
        OrganizationMember.objects.create(organization=org, user=deleter, role=OWNER)

        start = threading.Barrier(2)
        outcomes = {}

        def run(name, action):
            try:
                start.wait(timeout=10)
                action()
                outcomes[name] = "done"
            except Exception as exc:
                outcomes[name] = type(exc).__name__
            finally:
                connection.close()

        actor = Actor(
            actor_type="organization",
            organization=org,
            organization_member=leaver_member,
        )
        threads = [
            threading.Thread(
                target=run, args=("leave", lambda: OrganizationMemberService.leave(actor))
            ),
            threading.Thread(
                target=run,
                args=(
                    "delete",
                    lambda: confirm_account_deletion(deleter, password="pass1234"),
                ),
            ),
        ]

        with patch(f"{SERVICE}.safe_group_send"):
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)

        self.assertEqual(sorted(outcomes.values()).count("done"), 1, outcomes)
        self.assertTrue(
            OrganizationMember.objects.filter(
                organization=org,
                role=OWNER,
                user__is_active=True,
                user__deletion_requested_at__isnull=True,
            ).exists(),
            outcomes,
        )


# ---------------------------------------------------------------------
# 0.2 A manager is looked up again after the lock
# ---------------------------------------------------------------------


class ManagerAfterLockTests(MemberFixesBase):
    """The second owner lost their power while this request waited on the lock."""

    def _lose_power_on_lock(self, **change):
        manager_id = self.second_owner_member.id

        def lose_power_then_lock(organization):
            OrganizationMember.objects.filter(id=manager_id).update(**change)
            return _lock_members(organization)

        return patch(f"{SERVICE}._lock_members", side_effect=lose_power_then_lock)

    def _assert_refused(self, response):
        self._assert_error(response, "forbidden", 403)
        self.assertEqual(response.data["message"], MANAGER_LOST_MESSAGE)

    LOST_POWER = (
        {"role": COACH},
        {"status": OrganizationMember.Status.REMOVED},
    )

    def test_change_role(self):
        for change in self.LOST_POWER:
            with self.subTest(change), self._lose_power_on_lock(**change):
                response = self._org_post(
                    self.second_owner,
                    f"members/{self.coach_member.id}/role",
                    {"role": "owner"},
                )
                self._assert_refused(response)

        self.coach_member.refresh_from_db()
        self.assertEqual(self.coach_member.role, COACH)

    def test_remove(self):
        for change in self.LOST_POWER:
            with self.subTest(change), self._lose_power_on_lock(**change):
                response = self._org_post(
                    self.second_owner, f"members/{self.coach_member.id}/remove"
                )
                self._assert_refused(response)

        self.coach_member.refresh_from_db()
        self.assertEqual(self.coach_member.status, OrganizationMember.Status.ACTIVE)

    def test_invite(self):
        for change in self.LOST_POWER:
            with self.subTest(change), self._lose_power_on_lock(**change):
                response = self._invite(self.second_owner, email="new@else.example")
                self._assert_refused(response)

        self.assertFalse(OrganizationInvite.objects.exists())
        self.mail.assert_not_called()

    def test_cancel(self):
        invite = self._pending_invite(self.outsider)

        for change in self.LOST_POWER:
            with self.subTest(change), self._lose_power_on_lock(**change):
                response = self._org_post(
                    self.second_owner, f"members/invites/{invite.id}/cancel"
                )
                self._assert_refused(response)

        invite.refresh_from_db()
        self.assertEqual(invite.status, OrganizationInvite.Status.PENDING)


# ---------------------------------------------------------------------
# 0.3 An invite ends with its sender's power
# ---------------------------------------------------------------------


class InviteEndsWithPowerTests(MemberFixesBase):
    def _their_invite(self):
        return self._pending_invite(
            self.outsider, invited_by=self.second_owner_member, role=OWNER
        )

    def _assert_cancelled(self, invite):
        invite.refresh_from_db()
        self.assertEqual(invite.status, OrganizationInvite.Status.CANCELLED)
        self.assertIsNotNone(invite.responded_at)

    def test_removing_an_owner_cancels_what_they_sent(self):
        invite = self._their_invite()
        other = self._pending_invite(self.staff, invited_by=self.owner_member)

        self._org_post(self.owner, f"members/{self.second_owner_member.id}/remove")

        self._assert_cancelled(invite)
        other.refresh_from_db()
        self.assertEqual(other.status, OrganizationInvite.Status.PENDING)

    def test_demoting_an_owner_cancels_what_they_sent(self):
        invite = self._their_invite()

        self._org_post(
            self.owner,
            f"members/{self.second_owner_member.id}/role",
            {"role": "coach"},
        )

        self._assert_cancelled(invite)

    def test_a_leaving_owner_cancels_what_they_sent(self):
        invite = self._their_invite()

        self._org_post(self.second_owner, "members/leave")

        self._assert_cancelled(invite)

    def test_an_invite_from_someone_removed_cannot_be_accepted(self):
        invite = self._their_invite()
        # A row the cancel missed: the check at accept must hold by itself.
        OrganizationMember.objects.filter(id=self.second_owner_member.id).update(
            status=OrganizationMember.Status.REMOVED
        )

        response = self._user_post(self.outsider, f"invites/{invite.id}/accept")

        self._assert_error(response, "invite_not_pending")
        self.assertFalse(
            OrganizationMember.all_objects.filter(user=self.outsider).exists()
        )

    def test_an_owner_invite_from_a_non_owner_cannot_be_accepted(self):
        self._grant_manage(self.coach_member)
        invite = self._pending_invite(
            self.outsider, invited_by=self.coach_member, role=OWNER
        )

        response = self._user_post(self.outsider, f"invites/{invite.id}/accept")

        self._assert_error(response, "invite_not_pending")

    def test_an_owner_invite_from_a_demoted_owner_cannot_be_accepted(self):
        invite = self._their_invite()
        OrganizationMember.objects.filter(id=self.second_owner_member.id).update(
            role=COACH
        )

        response = self._user_post(self.outsider, f"invites/{invite.id}/accept")

        self._assert_error(response, "invite_not_pending")

    def test_an_invite_from_a_deactivated_account_cannot_be_accepted(self):
        invite = self._their_invite()
        self.second_owner.is_active = False
        self.second_owner.save(update_fields=["is_active"])

        response = self._user_post(self.outsider, f"invites/{invite.id}/accept")

        self._assert_error(response, "invite_not_pending")

    def test_an_ordinary_invite_from_a_grant_holder_still_works(self):
        self._grant_manage(self.coach_member)
        invite = self._pending_invite(
            self.outsider, invited_by=self.coach_member, role=STAFF
        )

        response = self._user_post(self.outsider, f"invites/{invite.id}/accept")

        self.assertEqual(response.status_code, 200, response.data)


# ---------------------------------------------------------------------
# 0.4 Only owners touch ownership
# ---------------------------------------------------------------------


class OwnershipRulesTests(MemberFixesBase):
    def setUp(self):
        super().setUp()
        self._grant_manage(self.coach_member)

    def test_a_grant_cannot_make_an_owner(self):
        response = self._org_post(
            self.coach, f"members/{self.staff_member.id}/role", {"role": "owner"}
        )

        self._assert_error(response, "forbidden", 403)
        self.staff_member.refresh_from_db()
        self.assertEqual(self.staff_member.role, STAFF)

    def test_a_grant_cannot_demote_an_owner(self):
        response = self._org_post(
            self.coach, f"members/{self.owner_member.id}/role", {"role": "staff"}
        )

        self._assert_error(response, "forbidden", 403)
        self.owner_member.refresh_from_db()
        self.assertEqual(self.owner_member.role, OWNER)

    def test_a_grant_cannot_remove_an_owner(self):
        response = self._org_post(self.coach, f"members/{self.owner_member.id}/remove")

        self._assert_error(response, "forbidden", 403)
        self.owner_member.refresh_from_db()
        self.assertEqual(self.owner_member.status, OrganizationMember.Status.ACTIVE)

    def test_a_grant_cannot_invite_an_owner(self):
        response = self._invite(self.coach, email="new@else.example", role="owner")

        self._assert_error(response, "forbidden", 403)
        self.assertFalse(OrganizationInvite.objects.exists())

    def test_a_grant_still_manages_everyone_else(self):
        changed = self._org_post(
            self.coach, f"members/{self.staff_member.id}/role", {"role": "coach"}
        )
        invited = self._invite(self.coach, email="new@else.example", role="staff")
        removed = self._org_post(self.coach, f"members/{self.staff_member.id}/remove")

        self.assertEqual(changed.status_code, 200, changed.data)
        self.assertEqual(invited.status_code, 201, invited.data)
        self.assertEqual(removed.status_code, 200, removed.data)

    def test_nobody_changes_their_own_role(self):
        for user, member in (
            (self.owner, self.owner_member),
            (self.coach, self.coach_member),
        ):
            with self.subTest(user=user.username):
                response = self._org_post(
                    user, f"members/{member.id}/role", {"role": "staff"}
                )
                self._assert_error(response, "cannot_change_own_role")

        self.owner_member.refresh_from_db()
        self.assertEqual(self.owner_member.role, OWNER)


# ---------------------------------------------------------------------
# 0.5 One throttle bucket per org
# ---------------------------------------------------------------------


class InviteThrottleKeyTests(MemberFixesBase):
    def _spellings(self):
        org_id = self.org.id
        return [
            str(org_id),
            str(org_id).upper(),
            org_id.hex,
            "{" + str(org_id) + "}",
            f"urn:uuid:{org_id}",
        ]

    def _post_as(self, user, org_header, email):
        self.client.force_authenticate(user=user)
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(
                "/organizations/members/invite",
                {"email": email, "role": "coach"},
                format="json",
                HTTP_X_ACTOR_TYPE="organization",
                HTTP_X_ACTOR_ID=org_header,
            )

    def test_every_spelling_of_the_uuid_reads_the_same(self):
        self.assertEqual(
            {canonical_actor_id(spelling) for spelling in self._spellings()},
            {str(self.org.id)},
        )

    def test_something_that_is_not_a_uuid_stays_as_it_is(self):
        self.assertEqual(canonical_actor_id("not-a-uuid"), "not-a-uuid")

    def test_a_new_spelling_is_not_a_new_bucket(self):
        for spelling in self._spellings()[1:]:
            cache.clear()
            with self.subTest(spelling), patch.object(
                OrgInviteCreateThrottle, "rate", "1/day", create=True
            ):
                first = self._post_as(self.owner, str(self.org.id), f"{hash(spelling)}a@x.example")
                second = self._post_as(self.owner, spelling, f"{hash(spelling)}b@x.example")

                self.assertEqual(first.status_code, 201, first.data)
                self._assert_error(second, "throttled", 429)

    def test_two_owners_of_one_org_share_a_bucket(self):
        with patch.object(OrgInviteCreateThrottle, "rate", "1/day", create=True):
            first = self._invite(self.owner, email="one@else.example")
            second = self._invite(self.second_owner, email="two@else.example")

        self.assertEqual(first.status_code, 201, first.data)
        self._assert_error(second, "throttled", 429)

    def test_a_coachs_refused_attempts_do_not_use_up_the_owners_allowance(self):
        with patch.object(OrgInviteCreateThrottle, "rate", "1/day", create=True):
            refused = self._invite(self.coach, email="one@else.example")
            throttled = self._invite(self.coach, email="two@else.example")
            mine = self._invite(self.owner, email="three@else.example")

        self._assert_error(refused, "forbidden", 403)
        # Their own bucket ran out, and nobody else's.
        self._assert_error(throttled, "throttled", 429)
        self.assertEqual(mine.status_code, 201, mine.data)

    def test_a_coach_given_members_manage_draws_from_the_org_bucket(self):
        self._grant_manage(self.coach_member)

        with patch.object(OrgInviteCreateThrottle, "rate", "1/day", create=True):
            first = self._invite(self.coach, email="one@else.example")
            second = self._invite(self.owner, email="two@else.example")

        self.assertEqual(first.status_code, 201, first.data)
        self._assert_error(second, "throttled", 429)

    def test_a_removed_owner_is_back_in_a_bucket_of_their_own(self):
        OrganizationMember.objects.filter(id=self.second_owner_member.id).update(
            status=OrganizationMember.Status.REMOVED
        )

        with patch.object(OrgInviteCreateThrottle, "rate", "1/day", create=True):
            refused = self._invite(self.second_owner, email="one@else.example")
            mine = self._invite(self.owner, email="two@else.example")

        self.assertEqual(refused.status_code, 403, refused.data)
        self.assertEqual(mine.status_code, 201, mine.data)

    def test_an_outsider_cannot_spend_the_clubs_allowance(self):
        with patch.object(OrgInviteCreateThrottle, "rate", "1/day", create=True):
            refused = self._post_as(self.outsider, str(self.org.id), "x@else.example")
            mine = self._invite(self.owner, email="one@else.example")

        self.assertEqual(refused.status_code, 403, refused.data)
        self.assertEqual(mine.status_code, 201, mine.data)


# ---------------------------------------------------------------------
# 0.6 The announcement cannot be skipped
# ---------------------------------------------------------------------


class AnnouncementOutlivesNotificationsTests(MemberFixesBase):
    def _broken(self, method):
        return patch(
            f"{SERVICE}.NotificationService.{method}",
            side_effect=RuntimeError("notification insert failed"),
        )

    def _quietly(self):
        # The robust callback logs the failure, which is the point, not noise.
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)

    def test_a_removal_still_closes_the_socket(self):
        self._quietly()
        with self._broken("org_member_removed"):
            response = self._org_post(
                self.owner, f"members/{self.coach_member.id}/remove"
            )

        self.assertEqual(response.status_code, 200, response.data)
        announced = self._announced()
        self.assertEqual(len(announced), 1)
        self.assertEqual(announced[0]["user_id"], str(self.coach.id))
        self.assertEqual(announced[0]["status"], "removed")
        self.assertTrue(announced[0]["close"])

    def test_a_role_change_is_still_announced(self):
        self._quietly()
        with self._broken("org_role_changed"):
            self._org_post(
                self.owner, f"members/{self.coach_member.id}/role", {"role": "staff"}
            )

        self.assertEqual(self._announced()[0]["status"], "role_changed")

    def test_an_accept_is_still_announced(self):
        self._quietly()
        invite = self._pending_invite(self.outsider)

        with self._broken("org_invite_accepted"):
            self._user_post(self.outsider, f"invites/{invite.id}/accept")

        self.assertEqual(self._announced()[0]["status"], "active")

    def test_a_leave_is_announced_and_closes(self):
        self._org_post(self.coach, "members/leave")

        announced = self._announced()
        self.assertEqual(announced[0]["status"], "left")
        self.assertTrue(announced[0]["close"])


# ---------------------------------------------------------------------
# 0.7 Removed between the auth check and joining the group
# ---------------------------------------------------------------------


@override_settings(
    CHANNEL_LAYERS={"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}
)
class ConnectRaceTests(APITransactionTestCase):
    def setUp(self):
        cache.clear()
        self.coach = make_user("clubcoach", "Cora Coach")
        self.org = Organization.objects.create(
            name="Dream FC", username="dreamfc", type=Organization.Type.CLUB
        )
        self.member = OrganizationMember.objects.create(
            organization=self.org, user=self.coach, role=COACH
        )
        self.conversation = Conversation.objects.create(type=Conversation.Type.DIRECT)
        ConversationParticipant.objects.create(
            conversation=self.conversation, org=self.org, has_accepted=True
        )

    def _with_scope(self, communicator):
        # What the auth middleware built, before the removal below committed.
        communicator.scope["user"] = self.coach
        communicator.scope["actor"] = Actor(
            actor_type="organization",
            organization=self.org,
            organization_member=self.member,
        )
        communicator.scope["url_route"] = {
            "kwargs": {"conversation_id": str(self.conversation.id)}
        }
        return communicator

    def _notifications(self):
        return self._with_scope(
            WebsocketCommunicator(
                UserNotificationsConsumer.as_asgi(), "/ws/notifications/"
            )
        )

    def _chat(self):
        return self._with_scope(
            WebsocketCommunicator(ChatConsumer.as_asgi(), "/ws/chat/")
        )

    def _connects(self, communicator):
        async def scenario():
            connected, code = await communicator.connect()
            if connected:
                await communicator.disconnect()
            return connected, code

        return async_to_sync(scenario)()

    def test_a_current_member_connects(self):
        self.assertTrue(self._connects(self._notifications())[0])
        self.assertTrue(self._connects(self._chat())[0])

    def test_a_removed_members_sockets_close(self):
        OrganizationMember.objects.filter(id=self.member.id).update(
            status=OrganizationMember.Status.REMOVED
        )

        for name, communicator in (
            ("notifications", self._notifications()),
            ("chat", self._chat()),
        ):
            with self.subTest(name):
                connected, code = self._connects(communicator)

                self.assertFalse(connected)
                self.assertEqual(code, WS_CLOSE_ORG_ACCESS_REVOKED)


# ---------------------------------------------------------------------
# 0.8 Declining by token proves who you are
# ---------------------------------------------------------------------


class DeclineByTokenTests(MemberFixesBase):
    def _emailed_invite(self, address="new@else.example"):
        self._invite(email=address)
        return self._raw_token(), OrganizationInvite.objects.get(email=address)

    def test_the_wrong_account_is_refused_like_accept(self):
        token, invite = self._emailed_invite()

        response = self._user_post(self.outsider, f"invites/token/{token}/decline")

        self._assert_error(response, "email_mismatch", 403)
        self.assertIn("n*w@else.example", response.data["message"])
        invite.refresh_from_db()
        self.assertEqual(invite.status, OrganizationInvite.Status.PENDING)

    def test_the_address_owner_may_decline(self):
        token, invite = self._emailed_invite()
        owner_of_address = make_user("newperson", email="new@else.example")

        response = self._user_post(owner_of_address, f"invites/token/{token}/decline")

        self.assertEqual(response.status_code, 200, response.data)
        invite.refresh_from_db()
        self.assertEqual(invite.status, OrganizationInvite.Status.DECLINED)

    def test_an_unverified_address_owner_is_refused(self):
        token, _ = self._emailed_invite()
        unverified = make_user(
            "unverified", email="new@else.example", is_email_verified=False
        )

        response = self._user_post(unverified, f"invites/token/{token}/decline")

        self._assert_error(response, "email_mismatch", 403)

    def test_the_notice_names_the_invitee_by_masked_address(self):
        token, _ = self._emailed_invite()
        owner_of_address = make_user("newperson", "Nia New", email="new@else.example")

        self._user_post(owner_of_address, f"invites/token/{token}/decline")

        notice = self._notifications(self.owner, Notification.Type.ORG_INVITE_DECLINED).get()
        self.assertEqual(notice.data["member_name"], "n*w@else.example")

    def test_the_notice_names_the_invited_user_for_an_invite_by_id(self):
        self._invite(user_id=str(self.outsider.id))
        token = self._raw_token()

        self._user_post(self.outsider, f"invites/token/{token}/decline")

        notice = self._notifications(self.owner, Notification.Type.ORG_INVITE_DECLINED).get()
        self.assertEqual(notice.data["member_name"], "Otto Outsider")


# ---------------------------------------------------------------------
# 0.9 No account oracle
# ---------------------------------------------------------------------


class NoAccountOracleTests(MemberFixesBase):
    def _assert_looks_new_and_sends_nothing(self, response):
        """The same 201 an address with no account gets, and nothing goes out."""
        self.assertEqual(response.status_code, 201, response.data)

        unknown = self._invite(email="nobody-here@else.example")
        self.assertEqual(
            set(response.data["data"]["invite"]), set(unknown.data["data"]["invite"])
        )
        self.assertEqual(response.data["message"], unknown.data["message"])
        self.assertIsNone(response.data["data"]["invite"]["invited_user"])
        self.assertTrue(response.data["data"]["invite"]["email_masked"])

    def _deliveries_to(self, user):
        return [
            call
            for call in self.mail.call_args_list
            if call.kwargs.get("to_email") == user.email
        ]

    def _org_invite_notices(self, user):
        return self._notifications(user, Notification.Type.ORG_INVITE)

    def test_an_address_belonging_to_a_member(self):
        response = self._invite(email=self.coach.email)

        self._assert_looks_new_and_sends_nothing(response)
        self.assertEqual(self._deliveries_to(self.coach), [])
        self.assertFalse(self._org_invite_notices(self.coach).exists())

    def test_an_address_belonging_to_someone_invited_by_id(self):
        self._invite(user_id=str(self.outsider.id))
        self.mail.reset_mock()
        notices = self._org_invite_notices(self.outsider).count()

        response = self._invite(email=self.outsider.email)

        self._assert_looks_new_and_sends_nothing(response)
        self.assertEqual(self._deliveries_to(self.outsider), [])
        self.assertEqual(self._org_invite_notices(self.outsider).count(), notices)

    def test_an_address_on_the_other_side_of_a_block(self):
        Block.objects.create(blocker_user=self.outsider, blocked_org=self.org)
        cache.clear()

        response = self._invite(email=self.outsider.email)

        self._assert_looks_new_and_sends_nothing(response)
        self.assertEqual(self._deliveries_to(self.outsider), [])
        self.assertFalse(self._org_invite_notices(self.outsider).exists())

    def test_the_row_shows_on_the_pending_list_like_any_other(self):
        self._invite(email=self.coach.email)

        pending = self._org_get(self.owner, "members/invites").data["data"]["invites"]

        self.assertEqual(len(pending), 1)
        self.assertIsNone(pending[0]["invited_user"])
        self.assertTrue(pending[0]["email_masked"])

    def test_an_address_already_on_the_list_is_told_whoever_owns_it(self):
        self._invite(email="new@else.example")
        self.assertEqual(
            self._invite(email="new@else.example").data["data"]["code"],
            "already_invited",
        )

        self._invite(email=self.outsider.email)
        self.assertEqual(
            self._invite(email=self.outsider.email).data["data"]["code"],
            "already_invited",
        )

    def test_by_id_reports_an_invite_made_by_id(self):
        self._invite(user_id=str(self.outsider.id))

        response = self._invite(user_id=str(self.outsider.id))

        self._assert_error(response, "already_invited")

    def test_by_id_says_nothing_about_a_claimed_email_invite(self):
        # They were invited by address, then verified that address. Refusing here
        # would tell the inviter whose address it was.
        self._invite(email="new@else.example")
        person = make_user("newperson", email="new@else.example")
        OrganizationMemberService.claim_email_invites(person)
        claimed = OrganizationInvite.objects.get(email="new@else.example")
        self.assertEqual(claimed.invited_user, person)
        self.mail.reset_mock()

        response = self._invite(user_id=str(person.id))

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(
            response.data["data"]["invite"]["invited_user"]["id"], str(person.id)
        )
        self.mail.assert_called_once()

        claimed.refresh_from_db()
        self.assertIsNone(claimed.invited_user)
        self.assertEqual(claimed.status, OrganizationInvite.Status.PENDING)

    def test_a_former_members_address_is_a_normal_invite(self):
        OrganizationMember.objects.filter(id=self.staff_member.id).update(
            status=OrganizationMember.Status.REMOVED
        )

        response = self._invite(email=self.staff.email)

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(len(self._deliveries_to(self.staff)), 1)
        self.assertTrue(self._org_invite_notices(self.staff).exists())


# ---------------------------------------------------------------------
# 0.10 Claiming
# ---------------------------------------------------------------------


class ClaimRaceTests(MemberFixesBase):
    def setUp(self):
        super().setUp()
        self.person = make_user("newperson", email="new@else.example")

    def _emailed(self, org=None):
        return OrganizationInvite.objects.create(
            organization=org or self.org,
            role=COACH,
            invited_by=self.owner_member,
            email="new@else.example",
            token_hash=f"{(org or self.org).id}".replace("-", "") + "0" * 32,
            expires_at=timezone.now() + timedelta(days=14),
        )

    def test_an_invite_cancelled_after_the_list_was_read_is_skipped(self):
        invite = self._emailed()
        stale = OrganizationInvite.objects.get(id=invite.id)
        OrganizationInvite.objects.filter(id=invite.id).update(
            status=OrganizationInvite.Status.CANCELLED
        )

        claimed = OrganizationMemberService._claim_one(stale, self.person)

        self.assertFalse(claimed)
        invite.refresh_from_db()
        self.assertIsNone(invite.invited_user)
        self.assertFalse(
            self._notifications(self.person, Notification.Type.ORG_INVITE).exists()
        )

    def test_one_failure_does_not_stop_the_rest(self):
        other_org = Organization.objects.create(
            name="Rival FC", username="rivalfc", type=Organization.Type.CLUB
        )
        first = self._emailed()
        second = self._emailed(other_org)

        real = OrganizationMemberService._claim_one
        calls = []

        def fail_first(invite, user):
            calls.append(invite.id)
            if len(calls) == 1:
                raise RuntimeError("boom")
            return real(invite, user)

        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)

        with patch.object(OrganizationMemberService, "_claim_one", side_effect=fail_first):
            count = OrganizationMemberService.claim_email_invites(self.person)

        self.assertEqual(count, 1)
        self.assertEqual(set(calls), {first.id, second.id})


# ---------------------------------------------------------------------
# 0.11 The Django admin
# ---------------------------------------------------------------------


# These render real admin pages, and the Manifest storage in settings raises on a
# static file that collectstatic has not written, which is every file on a fresh
# checkout. The plain storage resolves the same URLs without a manifest.
@override_settings(
    STORAGES={
        **settings.STORAGES,
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
        },
    }
)
class MemberAdminTests(MemberFixesBase):
    def setUp(self):
        super().setUp()
        self.admin = OrganizationMemberAdmin(OrganizationMember, AdminSite())
        self.request = RequestFactory().post("/admin/")
        self.request.user = make_user("staffer", is_staff=True, is_superuser=True)

    def _form(self, user):
        form_class = self.admin.get_form(self.request, obj=None)
        now = timezone.now()
        return form_class(
            data={
                "organization": str(self.org.id),
                "user": str(user.id),
                "role": "coach",
                "joined_at_0": now.strftime("%Y-%m-%d"),
                "joined_at_1": now.strftime("%H:%M:%S"),
                "permission_overrides": "{}",
            }
        )

    def _admin_url(self, name, *args):
        return reverse(f"admin:organization_{name}", args=args)

    def test_an_existing_row_cannot_be_moved_or_have_its_role_edited(self):
        fields = self.admin.get_readonly_fields(self.request, self.coach_member)

        for field in ("organization", "user", "role", "status"):
            self.assertIn(field, fields)

    def test_a_new_row_picks_its_org_person_and_role_but_not_a_status(self):
        fields = self.admin.get_readonly_fields(self.request, None)

        for field in ("organization", "user", "role"):
            self.assertNotIn(field, fields)
        self.assertIn("status", fields)

    def test_the_org_pages_inline_cannot_edit_add_or_delete(self):
        inline = OrganizationMemberInline(OrganizationMember, AdminSite())

        self.assertIn("role", inline.readonly_fields)
        self.assertIn("status", inline.readonly_fields)
        self.assertFalse(inline.has_add_permission(self.request))
        self.assertFalse(inline.has_delete_permission(self.request))

    def test_the_change_page_shows_organization_and_user_read_only(self):
        self.client.force_login(self.request.user)

        page = self.client.get(
            self._admin_url("organizationmember_change", self.coach_member.id)
        )

        self.assertEqual(page.status_code, 200)
        html = page.content.decode()
        self.assertNotIn('name="organization"', html)
        self.assertNotIn('name="user"', html)
        self.assertNotIn('name="role"', html)
        self.assertIn("Dream FC", html)

    def test_a_post_cannot_move_a_row_anyway(self):
        self.client.force_login(self.request.user)
        other = Organization.objects.create(
            name="Rival FC", username="rivalfc", type=Organization.Type.CLUB
        )
        now = timezone.now()

        self.client.post(
            self._admin_url("organizationmember_change", self.coach_member.id),
            {
                "organization": str(other.id),
                "user": str(self.outsider.id),
                "role": "owner",
                "joined_at_0": now.strftime("%Y-%m-%d"),
                "joined_at_1": now.strftime("%H:%M:%S"),
                "permission_overrides": "{}",
            },
        )

        self.coach_member.refresh_from_db()
        self.assertEqual(self.coach_member.organization_id, self.org.id)
        self.assertEqual(self.coach_member.user_id, self.coach.id)
        self.assertEqual(self.coach_member.role, COACH)

    def test_the_add_page_still_offers_all_three(self):
        self.client.force_login(self.request.user)

        html = self.client.get(self._admin_url("organizationmember_add")).content.decode()

        for field in ("organization", "user", "role"):
            self.assertIn(f'name="{field}"', html)

    def test_the_delete_view_is_403(self):
        self.client.force_login(self.request.user)
        url = self._admin_url("organizationmember_delete", self.coach_member.id)

        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.post(url, {"post": "yes"}).status_code, 403)
        self.assertTrue(
            OrganizationMember.all_objects.filter(id=self.coach_member.id).exists()
        )

    def test_the_bulk_delete_action_is_gone(self):
        self.client.force_login(self.request.user)

        html = self.client.get(self._admin_url("organizationmember_changelist")).content.decode()

        self.assertNotIn("delete_selected", html)

    def test_the_org_list_offers_is_verified_as_an_editable_column(self):
        self.client.force_login(self.request.user)

        page = self.client.get(self._admin_url("organization_changelist"))

        self.assertEqual(page.status_code, 200)
        self.assertIn("is_verified", page.context["cl"].list_editable)
        self.assertContains(page, 'name="form-0-is_verified"')

    def test_the_org_page_offers_no_delete_on_a_member(self):
        self.client.force_login(self.request.user)

        page = self.client.get(self._admin_url("organization_change", self.org.id))

        self.assertEqual(page.status_code, 200)
        self.assertNotIn("members-0-DELETE", page.content.decode())

    def test_an_invite_is_read_only_and_cannot_be_deleted(self):
        invite = self._pending_invite(self.outsider)
        self.client.force_login(self.request.user)
        change = self._admin_url("organizationinvite_change", invite.id)

        html = self.client.get(change).content.decode()

        for field in ("status", "organization", "email", "token_hash"):
            self.assertNotIn(f'name="{field}"', html)
        self.assertNotIn(invite.token_hash, html)

        self.client.post(change, {"status": "accepted"})
        invite.refresh_from_db()
        self.assertEqual(invite.status, OrganizationInvite.Status.PENDING)

        delete = self._admin_url("organizationinvite_delete", invite.id)
        self.assertEqual(self.client.get(delete).status_code, 403)
        self.assertTrue(OrganizationInvite.objects.filter(id=invite.id).exists())

    def test_re_adding_a_removed_person_is_a_form_error_not_a_500(self):
        OrganizationMember.objects.filter(id=self.staff_member.id).update(
            status=OrganizationMember.Status.REMOVED
        )

        form = self._form(self.staff)

        self.assertFalse(form.is_valid())
        self.assertIn("already has a row", str(form.non_field_errors()))

    def test_adding_somebody_new_is_fine(self):
        form = self._form(self.outsider)

        self.assertTrue(form.is_valid(), form.errors)


# ---------------------------------------------------------------------
# 0.12 Tokens out of logs and Sentry
# ---------------------------------------------------------------------


class TokenRedactionTests(SimpleTestCase):
    TOKEN = "AbC-123_xYz"

    PATHS = (
        f"/organizations/invites/token/{TOKEN}",
        f"/organizations/invites/token/{TOKEN}/accept",
        f"/organizations/invites/token/{TOKEN}/decline",
        f"/guardian/consent/{TOKEN}",
        f"/guardian/consent/{TOKEN}/approve",
    )

    def _assert_clean(self, value):
        self.assertNotIn(self.TOKEN, str(value))
        self.assertIn("[redacted]", str(value))

    def test_before_send_covers_request_transaction_and_exception(self):
        for path in self.PATHS:
            with self.subTest(path):
                event = sentry_redact_event(
                    {
                        "transaction": path,
                        "request": {"url": f"https://api.goatza.com{path}?a=1"},
                        "exception": {"values": [{"value": f"Failed on {path}"}]},
                        "extra": {"sys.argv": [f"https://x{path}"]},
                    },
                    {},
                )

                self._assert_clean(event["transaction"])
                self._assert_clean(event["request"]["url"])
                self._assert_clean(event["exception"]["values"][0]["value"])
                self._assert_clean(event["extra"]["sys.argv"][0])

    def test_before_send_covers_the_raw_token_variable(self):
        event = sentry_redact_event(
            {"frames": [{"vars": {"raw_token": f"'{self.TOKEN}'"}}]}, {}
        )

        self._assert_clean(event)

    def test_a_mail_body_in_a_tasks_arguments_is_covered(self):
        event = sentry_redact_event(
            {"extra": {"celery-job": {"kwargs": {
                "message": f"View invite: https://goatza.com/org-invite/{self.TOKEN}\n\n",
            }}}},
            {},
        )

        self._assert_clean(event)

    def test_before_breadcrumb(self):
        crumb = sentry_redact_breadcrumb(
            {"category": "httplib", "data": {"url": f"https://x{self.PATHS[0]}"}}, {}
        )

        self._assert_clean(crumb)

    def test_the_rest_of_an_event_is_left_alone(self):
        event = {
            "request": {"url": "https://x/organizations/invites/mine"},
            "level": "error",
            "n": 3,
        }

        self.assertEqual(sentry_redact_event(event, {}), event)

    def _filtered(self, msg, args=()):
        record = logging.LogRecord("django.request", logging.WARNING, "x", 1, msg, args, None)
        self.assertTrue(RedactTokenPathsFilter().filter(record))
        return record.getMessage()

    def test_the_log_filter_redacts_both_paths(self):
        for path in self.PATHS:
            with self.subTest(path):
                # Django's own shapes: a request line, and a formatted path.
                self._assert_clean(self._filtered('"%s" %d %d', (f"GET {path} HTTP/1.1", 200, 12)))
                self._assert_clean(self._filtered("Not Found: %s", (path,)))

    def test_the_log_filter_leaves_other_lines_alone(self):
        self.assertEqual(
            self._filtered("Not Found: %s", ("/guardian/details",)),
            "Not Found: /guardian/details",
        )

    def test_a_line_with_a_broken_format_string_still_logs(self):
        record = logging.LogRecord("x", logging.INFO, "x", 1, "%d", ("nope",), None)

        self.assertTrue(RedactTokenPathsFilter().filter(record))

    def test_every_console_line_goes_through_the_filter(self):
        logging_settings = settings.LOGGING

        self.assertIn(
            "redact_token_paths", logging_settings["handlers"]["console"]["filters"]
        )
        for logger in ("django", "django.request", "django.server"):
            self.assertEqual(logging_settings["loggers"][logger]["handlers"], ["console"])
        self.assertEqual(logging_settings["root"]["handlers"], ["console"])

    def test_the_filter_is_live_on_the_real_handler(self):
        handlers = logging.getLogger("django.server").handlers

        self.assertTrue(
            any(
                isinstance(f, RedactTokenPathsFilter)
                for handler in handlers
                for f in handler.filters
            )
        )

