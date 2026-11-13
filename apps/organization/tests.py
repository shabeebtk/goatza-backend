"""
Organization members: permissions, invites, removal, and losing access.

The side effects are real right up to the transport: a notification row is written
and a push job is handed to `enqueue`, which is mocked, as is the email sender.
Everything else goes through the same views and services production does.
"""

import hashlib
import importlib
import json
import re
from datetime import timedelta
from unittest.mock import patch

from asgiref.sync import async_to_sync, sync_to_async
from channels.testing import WebsocketCommunicator
from django.apps import apps as django_apps
from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.test import override_settings
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied
from rest_framework.test import APITestCase, APITransactionTestCase
from rest_framework_simplejwt.tokens import AccessToken

from apps.accounts.models import User, UserProfile
from apps.accounts.services.account_deletion_service import sole_owned_organizations
from apps.accounts.services.email_change_service import (
    EMAIL_CHANGE_OTP_PURPOSE,
    confirm_email_change,
)
from apps.achievements.services.achievement_verification_services import (
    AchievementVerificationService,
)
from apps.careers.services.career_verification_services import CareerVerificationService
from apps.guardians.testing import grant_guardian_consent
from apps.legal.testing import accept_current_terms
from apps.media.services.publication_service import PublicationService
from apps.messaging.consumers.chat_consumers import ChatConsumer
from apps.messaging.consumers.notification_consumers import UserNotificationsConsumer
from apps.messaging.models import Conversation, ConversationParticipant
from apps.moderation.models import Block
from apps.moderation.services.block_services import BlockService
from apps.notifications.models import Notification
from apps.notifications.services.grouping_service import NotificationGroupingService
from apps.notifications.services.deeplink_service import build_notification_url
from apps.notifications.services.notification_service import (
    build_notification_payload,
    get_org_alert_recipients,
)
from apps.organization.models import (
    Organization,
    OrganizationInvite,
    OrganizationMember,
    OrganizationProfile,
)
from apps.organization.permissions import (
    OrgPermission,
    has_org_permission,
    member_permissions,
    members_with_permission,
    require_org_permission,
    validate_permission_overrides,
)
from apps.organization.serializers.organization_serializers import (
    OrganizationMiniSerializer,
)
from apps.organization.services.organization_member_service import (
    MAX_ACTIVE_MEMBERS,
    MAX_PENDING_INVITES,
    OrganizationMemberService,
    _announce_membership,
)
from apps.organization.services.organization_service import OrganizationService
from apps.organization.throttles import OrgInviteCreateThrottle
from apps.recruitments.models import (
    Recruitment,
    RecruitmentApplication,
    RecruitmentApplicationStatusHistory,
)
from apps.recruitments.serializers.application_serializers import (
    ApplicationStatusHistorySerializer,
)
from apps.recruitments.services.announcement_service import AnnouncementService
from apps.sports.models import Sport
from core.actor import Actor
from core.middlewares.jwt_protocol_auth import JWTProtocolAuthMiddleware
from utils.cache_keys import CacheKeys
from utils.otp_validation import generate_otp
from utils.realtime import WS_CLOSE_ORG_ACCESS_REVOKED
from utils.transactional_emails import new_applicant_alert_recipients

SERVICE = "apps.organization.services.organization_member_service"

CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}


def make_user(username, name=None, **fields):
    fields.setdefault("is_email_verified", True)
    user = User.objects.create_user(
        email=fields.pop("email", f"{username}@example.com"),
        password="pass1234",
        username=username,
        **fields,
    )
    accept_current_terms(user)
    # Its fixture mails a parent. A test run must never send anybody anything, and
    # this mock is its own, so it never shows up in what a test asserts on.
    with patch("utils.transactional_emails.send_email_async"):
        grant_guardian_consent(user)
    UserProfile.objects.create(user=user, name=name or username.title())
    return user


@override_settings(
    CHANNEL_LAYERS=CHANNEL_LAYERS,
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class MembersTestBase(APITestCase):
    """An owner, a coach, a staff member and an outsider at one club."""

    def setUp(self):
        cache.clear()

        self.owner = make_user("clubowner", "Olive Owner")
        self.coach = make_user("clubcoach", "Cora Coach")
        self.staff = make_user("clubstaff", "Sam Staff")
        self.outsider = make_user("outsider", "Otto Outsider")

        self.org = Organization.objects.create(
            name="Dream FC", username="dreamfc", type=Organization.Type.CLUB
        )
        OrganizationProfile.objects.create(
            organization=self.org, logo="https://media.example.com/logo.webp"
        )

        self.owner_member = self._member(self.owner, OrganizationMember.Role.OWNER)
        self.coach_member = self._member(self.coach, OrganizationMember.Role.COACH)
        self.staff_member = self._member(self.staff, OrganizationMember.Role.STAFF)

        self.mail = self._patch("utils.transactional_emails.send_email_async")
        self.push = self._patch("apps.notifications.services.notification_service.enqueue")
        self.realtime = self._patch(f"{SERVICE}.safe_group_send")

    def _patch(self, target):
        patcher = patch(target)
        mocked = patcher.start()
        self.addCleanup(patcher.stop)
        return mocked

    def _member(self, user, role, org=None):
        return OrganizationMember.objects.create(
            organization=org or self.org, user=user, role=role
        )

    def _actor(self, member):
        return Actor(
            actor_type="organization",
            organization=member.organization,
            organization_member=member,
        )

    def _as_org(self, user, org=None):
        self.client.force_authenticate(user=user)
        return {
            "HTTP_X_ACTOR_TYPE": "organization",
            "HTTP_X_ACTOR_ID": str((org or self.org).id),
        }

    def _org_get(self, user, path):
        return self.client.get(f"/organizations/{path}", **self._as_org(user))

    def _org_post(self, user, path, data=None):
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(
                f"/organizations/{path}", data or {}, format="json", **self._as_org(user)
            )

    def _user_post(self, user, path, data=None):
        self.client.force_authenticate(user=user)
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(
                f"/organizations/{path}", data or {}, format="json"
            )

    def _invite(self, inviter=None, **body):
        body.setdefault("role", "coach")
        return self._org_post(inviter or self.owner, "members/invite", body)

    def _raw_token(self):
        """The token out of the one place it goes: the email."""
        text = self.mail.call_args.kwargs["message"]
        return re.search(r"/org-invite/(\S+)", text).group(1)

    def _pending_invite(self, user=None, **fields):
        fields.setdefault("role", "coach")
        fields.setdefault("invited_by", self.owner_member)
        fields.setdefault("expires_at", timezone.now() + timedelta(days=14))
        fields.setdefault("token_hash", hashlib.sha256(str(fields).encode()).hexdigest())
        return OrganizationInvite.objects.create(
            organization=self.org, invited_user=user, **fields
        )

    def _notifications(self, user, kind):
        return Notification.objects.filter(recipient_user=user, type=kind)

    def _assert_error(self, response, code, status_code=400):
        self.assertEqual(response.status_code, status_code, response.data)
        self.assertFalse(response.data["success"])
        self.assertEqual(response.data["data"]["code"], code)


# ---------------------------------------------------------------------
# The data migration
# ---------------------------------------------------------------------


class RetireAdminRoleMigrationTests(MembersTestBase):

    def test_admin_becomes_owner_and_joined_at_is_backfilled(self):
        migration = importlib.import_module(
            "apps.organization.migrations.0014_retire_admin_role"
        )
        admin = self._member(self.outsider, "admin")
        OrganizationMember.objects.filter(id=admin.id).update(
            joined_at=timezone.now() + timedelta(days=30)
        )

        migration.retire_admin_role(django_apps, None)

        admin.refresh_from_db()
        self.coach_member.refresh_from_db()
        self.assertEqual(admin.role, OrganizationMember.Role.OWNER)
        self.assertEqual(admin.joined_at, admin.created_at)
        self.assertEqual(self.coach_member.role, OrganizationMember.Role.COACH)


# ---------------------------------------------------------------------
# The catalog
# ---------------------------------------------------------------------


class PermissionCatalogTests(MembersTestBase):

    def test_role_defaults(self):
        self.assertEqual(
            member_permissions(self.owner_member), frozenset(OrgPermission.values)
        )
        self.assertEqual(
            member_permissions(self.coach_member),
            frozenset({
                OrgPermission.MEMBERS_VIEW.value,
                OrgPermission.SQUADS_VIEW.value,
                OrgPermission.SQUADS_REVIEW.value,
            }),
        )
        self.assertEqual(
            member_permissions(self.staff_member),
            frozenset({
                OrgPermission.MEMBERS_VIEW.value,
                OrgPermission.SQUADS_VIEW.value,
            }),
        )

    def test_overrides_grant_and_revoke(self):
        self.coach_member.permission_overrides = {
            "grant": ["org.block"],
            "revoke": ["members.view"],
        }

        self.assertTrue(has_org_permission(self.coach_member, OrgPermission.ORG_BLOCK))
        self.assertFalse(has_org_permission(self.coach_member, OrgPermission.MEMBERS_VIEW))

    def test_an_owner_keeps_members_manage_and_loses_the_rest(self):
        self.owner_member.permission_overrides = {
            "revoke": ["members.manage", "org.privacy"]
        }

        self.assertTrue(has_org_permission(self.owner_member, OrgPermission.MEMBERS_MANAGE))
        self.assertFalse(has_org_permission(self.owner_member, OrgPermission.ORG_PRIVACY))

    def test_a_member_who_left_holds_nothing(self):
        self.owner_member.status = OrganizationMember.Status.LEFT
        self.assertEqual(member_permissions(self.owner_member), frozenset())
        self.assertEqual(member_permissions(None), frozenset())

    def test_overrides_are_validated_against_the_catalog(self):
        validate_permission_overrides({"grant": ["org.block"], "revoke": []})
        for bad in (
            {"grant": ["org.nonsense"]},
            {"revoke": "org.block"},
            {"other": []},
            [],
        ):
            with self.assertRaises(ValidationError):
                validate_permission_overrides(bad)

        self.coach_member.permission_overrides = {"grant": ["org.nonsense"]}
        with self.assertRaises(ValidationError):
            self.coach_member.full_clean()

    def test_require_org_permission_speaks_a_sentence(self):
        self.assertEqual(
            require_org_permission(
                self._actor(self.owner_member), OrgPermission.MEMBERS_MANAGE
            ),
            self.owner_member,
        )
        with self.assertRaisesMessage(PermissionDenied, "owners can manage members"):
            require_org_permission(
                self._actor(self.coach_member), OrgPermission.MEMBERS_MANAGE
            )
        with self.assertRaises(PermissionDenied):
            require_org_permission(
                Actor(actor_type="user", user=self.owner), OrgPermission.MEMBERS_VIEW
            )

    def test_members_with_permission_follows_overrides_and_status(self):
        second_owner = self._member(self.outsider, OrganizationMember.Role.OWNER)
        OrganizationMember.objects.filter(id=second_owner.id).update(
            permission_overrides={"revoke": ["org.block", "members.manage"]}
        )
        OrganizationMember.objects.filter(id=self.coach_member.id).update(
            permission_overrides={"grant": ["org.block"]}
        )
        OrganizationMember.objects.filter(id=self.staff_member.id).update(
            permission_overrides={"grant": ["org.block"]},
            status=OrganizationMember.Status.REMOVED,
        )

        blockers = members_with_permission(self.org, OrgPermission.ORG_BLOCK)
        self.assertCountEqual(blockers, [self.owner_member, self.coach_member])

        # A member with no overrides at all stays in the answer, and an owner's
        # members.manage survives a revoke here just as it does per member.
        managers = members_with_permission(self.org, OrgPermission.MEMBERS_MANAGE)
        self.assertCountEqual(managers, [self.owner_member, second_owner])

    def test_my_permissions_on_the_org_list(self):
        self.client.force_authenticate(user=self.coach)
        row = self.client.get("/organizations/list").data["data"][0]
        self.assertEqual(row["my_role"], "coach")
        self.assertEqual(
            row["my_permissions"], ["members.view", "squads.review", "squads.view"]
        )

        self.client.force_authenticate(user=self.owner)
        row = self.client.get("/organizations/list").data["data"][0]
        self.assertEqual(row["my_role"], "owner")
        self.assertEqual(row["my_permissions"], sorted(OrgPermission.values))

    def test_my_permissions_on_the_details_serializers(self):
        self.client.force_authenticate(user=self.coach)
        for kind in ("all", "mini"):
            data = self.client.get(
                "/organizations/details",
                {"organization_id": str(self.org.id), "type": kind},
            ).data["data"]
            self.assertEqual(data["my_role"], "coach", kind)
            self.assertEqual(
                data["my_permissions"],
                ["members.view", "squads.review", "squads.view"],
                kind,
            )

        self.client.force_authenticate(user=self.outsider)
        data = self.client.get(
            "/organizations/details",
            {"organization_id": str(self.org.id), "type": "all"},
        ).data["data"]
        self.assertIsNone(data["my_role"])
        self.assertEqual(data["my_permissions"], [])

    def test_the_nested_mini_serializer_carries_no_membership_fields(self):
        # It sits inside post and recruitment rows: fields here would be a query
        # per row of every feed.
        self.assertNotIn("my_role", OrganizationMiniSerializer(self.org).data)


# ---------------------------------------------------------------------
# Today's behaviour, unchanged
# ---------------------------------------------------------------------


class UnchangedGateTests(MembersTestBase):

    def _gate_refuses(self, gate, member):
        with self.assertRaises(PermissionDenied):
            gate(self._actor(member))

    def test_every_gate_lets_an_owner_through_and_nobody_else(self):
        gates = (
            AnnouncementService.require_reviewer,
            BlockService.require_block_permission,
            CareerVerificationService.require_reviewer,
            AchievementVerificationService.require_reviewer,
        )
        for gate in gates:
            gate(self._actor(self.owner_member))
            self._gate_refuses(gate, self.coach_member)
            self._gate_refuses(gate, self.staff_member)

    def test_the_privacy_setting(self):
        for user, expected in ((self.owner, 200), (self.coach, 403), (self.staff, 403)):
            response = self.client.patch(
                "/organizations/privacy/public-profile",
                {"is_public_profile": False},
                format="json",
                **self._as_org(user),
            )
            self.assertEqual(response.status_code, expected, user.username)

    def test_push_and_email_audiences_are_the_owners(self):
        self.assertEqual(get_org_alert_recipients(self.org), [self.owner])
        self.assertEqual(new_applicant_alert_recipients(self.org), [self.owner.email])


# ---------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------


class MembersListTests(MembersTestBase):

    def test_everyone_sees_the_list_owners_first(self):
        co_owner = self._member(self.outsider, OrganizationMember.Role.OWNER)

        data = self._org_get(self.staff, "members").data["data"]

        roles = [member["role"] for member in data["members"]]
        self.assertEqual(roles, ["owner", "owner", "coach", "staff"])
        self.assertEqual(
            data["me"], {"role": "staff", "permissions": ["members.view", "squads.view"]}
        )
        self.assertEqual(
            [member["is_me"] for member in data["members"]],
            [False, False, False, True],
        )
        self.assertIn(str(co_owner.id), [member["id"] for member in data["members"]])

    def test_a_row_has_the_agreed_shape(self):
        OrganizationMember.objects.filter(id=self.coach_member.id).update(
            added_by=self.owner_member
        )

        rows = self._org_get(self.owner, "members").data["data"]["members"]
        coach = next(row for row in rows if row["id"] == str(self.coach_member.id))

        self.assertEqual(
            set(coach), {"id", "user", "role", "joined_at", "added_by", "is_me"}
        )
        self.assertEqual(
            coach["user"],
            {
                "id": str(self.coach.id),
                "name": "Cora Coach",
                "username": "clubcoach",
                "profile_photo": "",
            },
        )
        self.assertEqual(
            coach["added_by"], {"name": "Olive Owner", "username": "clubowner"}
        )

    def test_a_person_acting_as_themselves_gets_no_list(self):
        self.client.force_authenticate(user=self.owner)
        self.assertEqual(self.client.get("/organizations/members").status_code, 403)

    def test_pending_invites_are_for_managers(self):
        self.assertEqual(self._org_get(self.coach, "members/invites").status_code, 403)

        self._invite(user_id=str(self.outsider.id))
        self._invite(email="someone@else.example")

        invites = self._org_get(self.owner, "members/invites").data["data"]["invites"]

        self.assertEqual(len(invites), 2)
        by_email = next(invite for invite in invites if invite["email_masked"])
        by_user = next(invite for invite in invites if invite["invited_user"])
        self.assertIsNone(by_email["invited_user"])
        self.assertIsNone(by_user["email_masked"])
        self.assertEqual(by_user["invited_user"]["username"], "outsider")
        self.assertEqual(by_user["invited_by"]["username"], "clubowner")


# ---------------------------------------------------------------------
# Sending an invite
# ---------------------------------------------------------------------


class SendInviteTests(MembersTestBase):

    def test_by_user_notifies_pushes_and_mails(self):
        response = self._invite(user_id=str(self.outsider.id), role="staff")

        self.assertEqual(response.status_code, 201, response.data)
        invite = OrganizationInvite.objects.get()
        self.assertEqual(invite.invited_user, self.outsider)
        self.assertEqual(invite.email, "")
        self.assertEqual(invite.role, "staff")
        self.assertEqual(invite.invited_by, self.owner_member)

        notification = self._notifications(self.outsider, Notification.Type.ORG_INVITE).get()
        self.assertEqual(notification.data["invite_id"], str(invite.id))
        self.assertEqual(notification.data["organization"]["name"], "Dream FC")
        self.assertEqual(self.push.call_count, 1)

        kwargs = self.mail.call_args.kwargs
        self.assertEqual(kwargs["to_email"], self.outsider.email)
        self.assertEqual(
            kwargs["subject"], "Olive Owner invited you to join Dream FC on Goatza"
        )
        self.assertIn("as Staff", kwargs["message"])
        self.assertIn("expires in 14 days", kwargs["message"])

    def test_only_the_tokens_hash_is_stored(self):
        self._invite(user_id=str(self.outsider.id))

        raw = self._raw_token()
        invite = OrganizationInvite.objects.get()

        self.assertEqual(invite.token_hash, hashlib.sha256(raw.encode()).hexdigest())
        self.assertNotEqual(invite.token_hash, raw)
        self.assertIn(raw, self.mail.call_args.kwargs["html_message"])

    def test_an_email_answers_the_same_whether_or_not_it_has_an_account(self):
        known = self._invite(email="Outsider@Example.com")
        unknown = self._invite(email="nobody@else.example")

        self.assertEqual(known.status_code, 201)
        self.assertEqual(unknown.status_code, 201)
        self.assertEqual(known.data["message"], unknown.data["message"])
        self.assertEqual(
            set(known.data["data"]["invite"]), set(unknown.data["data"]["invite"])
        )
        for response in (known, unknown):
            self.assertIsNone(response.data["data"]["invite"]["invited_user"])
            self.assertIsNotNone(response.data["data"]["invite"]["email_masked"])

    def test_an_existing_accounts_email_attaches_and_notifies_them(self):
        self._invite(email="Outsider@Example.com")

        invite = OrganizationInvite.objects.get()
        self.assertEqual(invite.invited_user, self.outsider)
        self.assertEqual(invite.email, "outsider@example.com")
        self.assertEqual(
            self._notifications(self.outsider, Notification.Type.ORG_INVITE).count(), 1
        )
        self.assertEqual(self.mail.call_args.kwargs["to_email"], "outsider@example.com")

    def test_an_unknown_email_gets_the_link_and_no_notification(self):
        self._invite(email="nobody@else.example")

        invite = OrganizationInvite.objects.get()
        self.assertIsNone(invite.invited_user)
        self.assertFalse(Notification.objects.filter(type=Notification.Type.ORG_INVITE).exists())
        self.assertEqual(self.mail.call_args.kwargs["to_email"], "nobody@else.example")

    def test_refuses_an_active_member_named_by_id(self):
        # By email it answers like any other address: test_member_fixes.NoAccountOracleTests.
        self._assert_error(self._invite(user_id=str(self.coach.id)), "already_member")

    def test_refuses_a_second_pending_invite(self):
        self._invite(user_id=str(self.outsider.id))
        self._assert_error(self._invite(user_id=str(self.outsider.id)), "already_invited")

        self._invite(email="nobody@else.example")
        self._assert_error(self._invite(email="NOBODY@else.example"), "already_invited")

    def test_self_blocked_and_inactive_read_the_same(self):
        blocked = make_user("blockedone")
        Block.objects.create(blocker_user=blocked, blocked_org=self.org)
        org_blocked = make_user("orgblocked")
        Block.objects.create(blocker_org=self.org, blocked_user=org_blocked)
        inactive = make_user("inactiveone", is_active=False)

        answers = [
            self._invite(user_id=str(user.id))
            for user in (self.owner, blocked, org_blocked, inactive)
        ]

        for response in answers:
            self._assert_error(response, "cannot_invite")
            self.assertEqual(response.data["message"], "Couldn't invite this person.")
        self.assertFalse(OrganizationInvite.objects.exists())
        self.mail.assert_not_called()

    def test_a_blocked_accounts_email_looks_sent_and_delivers_nothing(self):
        blocked = make_user("blockedone")
        Block.objects.create(blocker_user=blocked, blocked_org=self.org)

        response = self._invite(email=blocked.email)

        self.assertEqual(response.status_code, 201)
        self.assertIsNone(response.data["data"]["invite"]["invited_user"])
        self.mail.assert_not_called()
        self.push.assert_not_called()
        self.assertFalse(Notification.objects.filter(recipient_user=blocked).exists())
        # Not attached, so it can't show up among their invites either.
        self.assertFalse(OrganizationMemberService.my_invites(blocked).exists())

    def test_an_unverified_account_is_just_an_address(self):
        unverified = make_user("unverified", is_email_verified=False)

        self.assertEqual(self._invite(email=unverified.email).status_code, 201)

        self.assertIsNone(OrganizationInvite.objects.get().invited_user)
        self.assertFalse(Notification.objects.filter(recipient_user=unverified).exists())
        self.assertEqual(self.mail.call_count, 1)

    def test_the_limits_are_fifty_and_twenty(self):
        self.assertEqual(MAX_ACTIVE_MEMBERS, 50)
        self.assertEqual(MAX_PENDING_INVITES, 20)

    def test_refuses_over_the_member_cap(self):
        with patch(f"{SERVICE}.MAX_ACTIVE_MEMBERS", 3):
            self._assert_error(self._invite(user_id=str(self.outsider.id)), "member_limit")

    def test_refuses_over_the_pending_invite_cap(self):
        self._invite(email="one@else.example")

        with patch(f"{SERVICE}.MAX_PENDING_INVITES", 1):
            self._assert_error(self._invite(email="two@else.example"), "invite_limit")

    def test_an_expired_invite_counts_for_nothing_and_can_be_replaced(self):
        old = self._pending_invite(
            self.outsider, expires_at=timezone.now() - timedelta(days=1)
        )

        with patch(f"{SERVICE}.MAX_PENDING_INVITES", 1):
            response = self._invite(user_id=str(self.outsider.id))

        self.assertEqual(response.status_code, 201, response.data)
        old.refresh_from_db()
        self.assertEqual(old.status, OrganizationInvite.Status.EXPIRED)

    def test_coach_and_staff_are_refused(self):
        for user in (self.coach, self.staff):
            response = self._invite(inviter=user, user_id=str(self.outsider.id))
            self._assert_error(response, "forbidden", 403)
        self.assertFalse(OrganizationInvite.objects.exists())

    def test_exactly_one_target_and_a_valid_role(self):
        self._assert_error(self._invite(role="owner"), "invalid_target")
        self._assert_error(
            self._invite(user_id=str(self.outsider.id), email="a@b.example"),
            "invalid_target",
        )
        self._assert_error(
            self._invite(user_id=str(self.outsider.id), role="admin"), "invalid_role"
        )
        self._assert_error(self._invite(email="not-an-email"), "invalid_email")

    def test_the_raw_token_is_never_logged(self):
        with self.assertLogs(SERVICE, level="INFO") as logs:
            self._invite(user_id=str(self.outsider.id))
            raw = self._raw_token()
            self._user_post(self.outsider, f"invites/token/{raw}/accept")

        self.assertNotIn(raw, "\n".join(logs.output))

    def test_cancel(self):
        self._invite(user_id=str(self.outsider.id))
        invite = OrganizationInvite.objects.get()

        self._assert_error(
            self._org_post(self.coach, f"members/invites/{invite.id}/cancel"),
            "forbidden",
            403,
        )
        response = self._org_post(self.owner, f"members/invites/{invite.id}/cancel")
        self.assertEqual(response.status_code, 200, response.data)

        invite.refresh_from_db()
        self.assertEqual(invite.status, OrganizationInvite.Status.CANCELLED)
        self._assert_error(
            self._org_post(self.owner, f"members/invites/{invite.id}/cancel"),
            "invite_not_pending",
        )


# ---------------------------------------------------------------------
# Answering by id
# ---------------------------------------------------------------------


class AnswerInviteTests(MembersTestBase):

    def setUp(self):
        super().setUp()
        self.invite = self._pending_invite(self.outsider, role="staff")

    def test_my_invites_lists_only_open_ones(self):
        expired_org = Organization.objects.create(
            name="Gone FC", username="gonefc", type=Organization.Type.CLUB
        )
        OrganizationInvite.objects.create(
            organization=expired_org,
            role="coach",
            invited_user=self.outsider,
            token_hash="e" * 64,
            expires_at=timezone.now() - timedelta(seconds=1),
        )

        self.client.force_authenticate(user=self.outsider)
        invites = self.client.get("/organizations/invites/mine").data["data"]["invites"]

        self.assertEqual([invite["id"] for invite in invites], [str(self.invite.id)])
        self.assertEqual(invites[0]["organization"]["name"], "Dream FC")
        self.assertEqual(invites[0]["invited_by"]["username"], "clubowner")

    def test_invites_are_read_as_the_person(self):
        response = self.client.get(
            "/organizations/invites/mine", **self._as_org(self.owner)
        )
        self.assertEqual(response.status_code, 403)

    def test_accept_makes_a_member(self):
        response = self._user_post(self.outsider, f"invites/{self.invite.id}/accept")

        self.assertEqual(response.status_code, 200, response.data)
        organization = response.data["data"]["organization"]
        self.assertEqual(organization["id"], str(self.org.id))
        self.assertEqual(organization["my_role"], "staff")
        self.assertEqual(
            organization["my_permissions"], ["members.view", "squads.view"]
        )

        member = OrganizationMember.objects.get(organization=self.org, user=self.outsider)
        self.assertEqual(member.role, "staff")
        self.assertEqual(member.added_by, self.owner_member)

        self.invite.refresh_from_db()
        self.assertEqual(self.invite.status, OrganizationInvite.Status.ACCEPTED)
        self.assertEqual(self.invite.accepted_member, member)
        self.assertIsNotNone(self.invite.responded_at)

    def test_accept_tells_the_managers_and_the_persons_devices(self):
        self.push.reset_mock()

        self._user_post(self.outsider, f"invites/{self.invite.id}/accept")

        self.assertEqual(
            self._notifications(self.owner, Notification.Type.ORG_INVITE_ACCEPTED).count(), 1
        )
        self.assertFalse(
            Notification.objects.filter(
                type=Notification.Type.ORG_INVITE_ACCEPTED,
                recipient_user__in=[self.coach, self.staff, self.outsider],
            ).exists()
        )
        self.assertEqual(self.push.call_count, 1)

        group, event = self.realtime.call_args_list[0].args
        self.assertEqual(group, f"user_notifications_{self.outsider.id}")
        self.assertEqual(event["notification_type"], "org_membership_changed")
        self.assertEqual(event["status"], "active")
        self.assertEqual(event["organization_id"], str(self.org.id))
        self.assertEqual(event["role"], "staff")

    def test_decline_tells_the_inviter_in_app_only(self):
        self.push.reset_mock()

        response = self._user_post(self.outsider, f"invites/{self.invite.id}/decline")

        self.assertEqual(response.status_code, 200, response.data)
        self.invite.refresh_from_db()
        self.assertEqual(self.invite.status, OrganizationInvite.Status.DECLINED)
        self.assertEqual(
            self._notifications(self.owner, Notification.Type.ORG_INVITE_DECLINED).count(), 1
        )
        self.push.assert_not_called()
        self.assertFalse(
            OrganizationMember.all_objects.filter(user=self.outsider).exists()
        )

    def test_a_decline_of_an_email_invite_does_not_name_the_account(self):
        invite = self._pending_invite(
            make_user("hasaccount", "Real Name"), email="hasaccount@example.com"
        )

        self._user_post(invite.invited_user, f"invites/{invite.id}/decline")

        notification = (
            self._notifications(self.owner, Notification.Type.ORG_INVITE_DECLINED).get()
        )
        self.assertEqual(notification.data["member_name"], "h********t@example.com")

    def test_someone_elses_invite_is_not_found(self):
        for action in ("accept", "decline"):
            response = self._user_post(self.staff, f"invites/{self.invite.id}/{action}")
            self._assert_error(response, "not_found", 404)

    def test_expired_cancelled_and_answered_invites_are_closed(self):
        OrganizationInvite.objects.filter(id=self.invite.id).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        self._assert_error(
            self._user_post(self.outsider, f"invites/{self.invite.id}/accept"),
            "invite_not_pending",
        )

        OrganizationInvite.objects.filter(id=self.invite.id).update(
            expires_at=timezone.now() + timedelta(days=1),
            status=OrganizationInvite.Status.CANCELLED,
        )
        self._assert_error(
            self._user_post(self.outsider, f"invites/{self.invite.id}/accept"),
            "invite_not_pending",
        )
        self.assertFalse(
            OrganizationMember.all_objects.filter(user=self.outsider).exists()
        )

    def test_a_suspended_org_cannot_be_joined(self):
        Organization.objects.filter(id=self.org.id).update(is_suspended=True)
        self._assert_error(
            self._user_post(self.outsider, f"invites/{self.invite.id}/accept"),
            "invite_not_pending",
        )

    def test_the_member_cap_applies_again_on_accept(self):
        with patch(f"{SERVICE}.MAX_ACTIVE_MEMBERS", 3):
            self._assert_error(
                self._user_post(self.outsider, f"invites/{self.invite.id}/accept"),
                "member_limit",
            )

    def test_a_removed_member_reactivates_their_own_row(self):
        old = self._member(self.outsider, OrganizationMember.Role.COACH)
        OrganizationMember.objects.filter(id=old.id).update(
            status=OrganizationMember.Status.REMOVED,
            removed_at=timezone.now(),
            removed_by=self.owner_member,
            permission_overrides={"grant": ["org.block"]},
        )

        response = self._user_post(self.outsider, f"invites/{self.invite.id}/accept")

        self.assertEqual(response.status_code, 200, response.data)
        old.refresh_from_db()
        self.assertEqual(old.status, OrganizationMember.Status.ACTIVE)
        self.assertEqual(old.role, "staff")
        self.assertIsNone(old.removed_at)
        self.assertIsNone(old.removed_by)
        self.assertEqual(old.permission_overrides, {})
        self.assertEqual(old.added_by, self.owner_member)
        self.assertEqual(
            OrganizationMember.all_objects.filter(user=self.outsider).count(), 1
        )

    def test_accepting_closes_their_other_invite_to_the_same_org(self):
        second = self._pending_invite(email=self.outsider.email)

        self._user_post(self.outsider, f"invites/{self.invite.id}/accept")

        second.refresh_from_db()
        self.assertEqual(second.status, OrganizationInvite.Status.CANCELLED)


# ---------------------------------------------------------------------
# Answering by token
# ---------------------------------------------------------------------


class TokenInviteTests(MembersTestBase):

    def _email_invite(self, email="newperson@else.example"):
        self._invite(email=email)
        return self._raw_token()

    def _page(self, token):
        self.client.force_authenticate(user=None)
        return self.client.get(f"/organizations/invites/token/{token}")

    def test_the_page_is_public_and_has_the_agreed_shape(self):
        token = self._email_invite()

        response = self._page(token)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            response.data["data"],
            {
                "organization": {
                    "name": "Dream FC",
                    "username": "dreamfc",
                    "logo": "https://media.example.com/logo.webp",
                },
                "role": "coach",
                "invited_by_name": "Olive Owner",
                "email_masked": "n*******n@else.example",
                "status": "pending",
            },
        )

    def test_unknown_expired_used_and_cancelled_are_one_404(self):
        used = self._email_invite("used@else.example")
        OrganizationInvite.objects.filter(email="used@else.example").update(
            status=OrganizationInvite.Status.ACCEPTED
        )
        expired = self._email_invite("expired@else.example")
        OrganizationInvite.objects.filter(email="expired@else.example").update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        cancelled = self._email_invite("cancelled@else.example")
        OrganizationInvite.objects.filter(email="cancelled@else.example").update(
            status=OrganizationInvite.Status.CANCELLED
        )

        answers = [self._page(token) for token in ("nope", used, expired, cancelled)]

        for response in answers:
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.data, answers[0].data)
            self.assertEqual(response.data["data"]["code"], "not_found")

    def test_accept_with_the_verified_email(self):
        token = self._email_invite()
        person = make_user("newperson", email="NewPerson@else.example")

        response = self._user_post(person, f"invites/token/{token}/accept")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["organization"]["my_role"], "coach")
        self.assertTrue(
            OrganizationMember.objects.filter(organization=self.org, user=person).exists()
        )
        invite = OrganizationInvite.objects.get()
        self.assertEqual(invite.invited_user, person)
        self.assertEqual(invite.status, OrganizationInvite.Status.ACCEPTED)

    def test_the_wrong_account_is_told_where_it_went(self):
        token = self._email_invite()

        response = self._user_post(self.outsider, f"invites/token/{token}/accept")

        self._assert_error(response, "email_mismatch", 403)
        self.assertEqual(
            response.data["message"], "This invite was sent to n*******n@else.example."
        )
        self.assertFalse(
            OrganizationMember.objects.filter(user=self.outsider, organization=self.org).exists()
        )

    def test_an_unverified_address_does_not_prove_anything(self):
        token = self._email_invite()
        person = make_user(
            "newperson", email="newperson@else.example", is_email_verified=False
        )

        response = self._user_post(person, f"invites/token/{token}/accept")

        self._assert_error(response, "email_mismatch", 403)

    def test_the_invited_user_may_accept_their_own_link(self):
        self._invite(user_id=str(self.outsider.id))
        token = self._raw_token()

        response = self._user_post(self.outsider, f"invites/token/{token}/accept")

        self.assertEqual(response.status_code, 200, response.data)

    def test_used_and_unknown_tokens_answer_the_same_on_accept(self):
        token = self._email_invite()
        person = make_user("newperson", email="newperson@else.example")
        self._user_post(person, f"invites/token/{token}/accept")

        used = self._user_post(self.outsider, f"invites/token/{token}/accept")
        unknown = self._user_post(self.outsider, "invites/token/nope/accept")

        self._assert_error(used, "not_found", 404)
        self.assertEqual(used.data, unknown.data)

    def test_decline_is_for_the_invitee_and_works_once(self):
        token = self._email_invite()
        invitee = make_user("invitee", email=OrganizationInvite.objects.get().email)

        response = self._user_post(invitee, f"invites/token/{token}/decline")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            OrganizationInvite.objects.get().status, OrganizationInvite.Status.DECLINED
        )
        self._assert_error(
            self._user_post(invitee, f"invites/token/{token}/decline"),
            "not_found",
            404,
        )

    def test_a_login_is_required_to_answer(self):
        token = self._email_invite()
        self.client.force_authenticate(user=None)

        response = self.client.post(f"/organizations/invites/token/{token}/accept")

        self.assertEqual(response.status_code, 401)


class InviteThrottleTests(MembersTestBase):

    def test_the_token_page_stops_at_thirty_a_minute_per_ip(self):
        self.client.force_authenticate(user=None)

        answers = [
            self.client.get("/organizations/invites/token/nope").status_code
            for _ in range(31)
        ]

        self.assertEqual(answers[:30], [404] * 30)
        self.assertEqual(answers[30], 429)

    def test_a_session_buys_no_way_round_the_token_limit(self):
        for _ in range(30):
            self._user_post(self.outsider, "invites/token/nope/accept")

        response = self._user_post(self.outsider, "invites/token/nope/accept")

        self._assert_error(response, "throttled", 429)

    def test_invites_are_limited_per_org_actor_in_the_standard_envelope(self):
        with patch.object(OrgInviteCreateThrottle, "rate", "1/day", create=True):
            self.assertEqual(self._invite(email="one@else.example").status_code, 201)
            response = self._invite(email="two@else.example")

        self._assert_error(response, "throttled", 429)
        self.assertIn("retry_after", response.data["data"])
        self.assertEqual(OrganizationInvite.objects.count(), 1)

    def test_the_agreed_rates(self):
        rates = settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]

        self.assertEqual(rates["org_invites"], "30/day")
        self.assertEqual(rates["org_invite_token"], "30/min")


# ---------------------------------------------------------------------
# An address becoming verified
# ---------------------------------------------------------------------


class ClaimEmailInviteTests(MembersTestBase):

    def setUp(self):
        super().setUp()
        self.email = "newperson@else.example"
        self.invite = self._pending_invite(email=self.email)

    def _assert_claimed(self, user):
        self.invite.refresh_from_db()
        self.assertEqual(self.invite.invited_user, user)
        self.assertEqual(
            self._notifications(user, Notification.Type.ORG_INVITE).count(), 1
        )
        self.assertEqual(
            list(OrganizationMemberService.my_invites(user)), [self.invite]
        )

    @patch("apps.accounts.views.user_auth_views.send_welcome_email")
    def test_on_otp_signup(self, _welcome):
        person = make_user(
            "newperson", email=self.email, is_active=False, is_email_verified=False
        )
        otp = generate_otp(self.email)

        self.client.force_authenticate(user=None)
        response = self.client.post(
            "/user/verify/otp", {"email": self.email, "otp": otp}, format="json"
        )

        self.assertEqual(response.status_code, 200, response.data)
        self._assert_claimed(person)

    def _google_login(self, requests_mock, _login):
        cache.set("google_oauth_state_abc", True, timeout=300)
        requests_mock.post.return_value.json.return_value = {"access_token": "t"}
        requests_mock.get.return_value.json.return_value = {
            "email": self.email,
            "name": "New Person",
        }
        self.client.force_authenticate(user=None)
        return self.client.get(
            "/user/auth/google/callback", {"code": "c", "state": "abc"}
        )

    @patch("apps.accounts.views.user_google_auth_views.on_successful_login")
    @patch("apps.accounts.views.user_google_auth_views.requests")
    def test_on_google_signup(self, requests_mock, login):
        self._google_login(requests_mock, login)

        self._assert_claimed(User.objects.get(email=self.email))

    @patch("apps.accounts.views.user_google_auth_views.on_successful_login")
    @patch("apps.accounts.views.user_google_auth_views.requests")
    def test_on_google_login_of_an_account_that_was_not_verified(self, requests_mock, login):
        person = make_user("newperson", email=self.email, is_email_verified=False)

        self._google_login(requests_mock, login)

        self._assert_claimed(person)

    @patch("apps.accounts.services.email_change_service.send_email_changed_notice")
    def test_on_a_verified_email_change(self, _notice):
        cache.set(CacheKeys.email_change_pending(self.outsider.id), self.email, timeout=600)
        otp = generate_otp(self.email, purpose=EMAIL_CHANGE_OTP_PURPOSE)

        confirm_email_change(self.outsider, otp=otp)

        self._assert_claimed(self.outsider)

    def test_an_unverified_account_claims_nothing(self):
        person = make_user("newperson", email=self.email, is_email_verified=False)

        self.assertEqual(OrganizationMemberService.claim_email_invites(person), 0)

        self.invite.refresh_from_db()
        self.assertIsNone(self.invite.invited_user)

    def test_a_block_stops_the_claim(self):
        person = make_user("newperson", email=self.email)
        Block.objects.create(blocker_user=person, blocked_org=self.org)

        self.assertEqual(OrganizationMemberService.claim_email_invites(person), 0)

    def test_it_never_raises_into_a_login(self):
        person = make_user("newperson", email=self.email)

        with patch(f"{SERVICE}.NotificationService.org_invite", side_effect=RuntimeError):
            self.assertEqual(OrganizationMemberService.claim_email_invites(person), 0)


# ---------------------------------------------------------------------
# Changing someone
# ---------------------------------------------------------------------


class ChangeRoleTests(MembersTestBase):

    def test_change_role(self):
        response = self._org_post(
            self.owner, f"members/{self.coach_member.id}/role", {"role": "staff"}
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["member"]["role"], "staff")
        self.coach_member.refresh_from_db()
        self.assertEqual(self.coach_member.role, "staff")

        notification = self._notifications(self.coach, Notification.Type.ORG_ROLE_CHANGED).get()
        self.assertEqual(notification.data["role_label"], "Staff")
        self.assertEqual(self.push.call_count, 1)

    def test_a_role_change_tells_the_client_and_leaves_its_sockets_open(self):
        self._org_post(self.owner, f"members/{self.coach_member.id}/role", {"role": "staff"})

        calls = {call.args[0]: call.args[1] for call in self.realtime.call_args_list}
        personal = calls[f"user_notifications_{self.coach.id}"]
        self.assertEqual(personal["status"], "role_changed")
        self.assertEqual(personal["role"], "staff")
        self.assertFalse(calls[f"org_actor_sockets_{self.org.id}"]["close"])

    def test_refusals(self):
        self._assert_error(
            self._org_post(
                self.coach, f"members/{self.staff_member.id}/role", {"role": "coach"}
            ),
            "forbidden",
            403,
        )
        self._assert_error(
            self._org_post(
                self.owner, f"members/{self.staff_member.id}/role", {"role": "admin"}
            ),
            "invalid_role",
        )
        self._assert_error(
            self._org_post(
                self.owner,
                "members/00000000-0000-0000-0000-000000000000/role",
                {"role": "coach"},
            ),
            "not_found",
            404,
        )

    def test_the_last_working_owner_cant_stop_being_one(self):
        # Nobody changes their own role now, so this is the case that is left: the
        # only other owner is on their way out, and does not count.
        departing = make_user("departing")
        self._member(departing, OrganizationMember.Role.OWNER)
        departing.deletion_requested_at = timezone.now()
        departing.save(update_fields=["deletion_requested_at"])

        response = self._org_post(
            departing, f"members/{self.owner_member.id}/role", {"role": "coach"}
        )

        self._assert_error(response, "last_owner")
        self.assertEqual(response.data["message"], "Dream FC needs at least one owner.")

    def test_a_deactivated_owner_does_not_count_as_the_other_owner(self):
        ghost = make_user("ghost", is_active=False)
        self._member(ghost, OrganizationMember.Role.OWNER)

        response = self._org_post(self.owner, "members/leave")

        self._assert_error(response, "last_owner")

    def test_demoting_an_owner_tells_the_other_owners(self):
        second = make_user("secondowner", "Second Owner")
        second_member = self._member(second, OrganizationMember.Role.OWNER)
        third = make_user("thirdowner", "Third Owner")
        self._member(third, OrganizationMember.Role.OWNER)

        self._org_post(self.owner, f"members/{second_member.id}/role", {"role": "coach"})

        notification = self._notifications(third, Notification.Type.ORG_OWNER_CHANGED).get()
        self.assertEqual(
            build_notification_payload(notification)["title"],
            "Olive Owner removed Second Owner as owner of Dream FC",
        )
        self.assertFalse(
            self._notifications(self.owner, Notification.Type.ORG_OWNER_CHANGED).exists()
        )
        self.assertFalse(
            self._notifications(second, Notification.Type.ORG_OWNER_CHANGED).exists()
        )


class RemoveMemberTests(MembersTestBase):

    def test_remove_keeps_the_row(self):
        response = self._org_post(self.owner, f"members/{self.coach_member.id}/remove")

        self.assertEqual(response.status_code, 200, response.data)
        member = OrganizationMember.all_objects.get(id=self.coach_member.id)
        self.assertEqual(member.status, OrganizationMember.Status.REMOVED)
        self.assertEqual(member.removed_by, self.owner_member)
        self.assertIsNotNone(member.removed_at)
        self.assertFalse(OrganizationMember.objects.filter(id=member.id).exists())

    def test_the_person_is_told_in_app_without_a_push(self):
        self._org_post(self.owner, f"members/{self.coach_member.id}/remove")

        self.assertEqual(
            self._notifications(self.coach, Notification.Type.ORG_MEMBER_REMOVED).count(), 1
        )
        self.push.assert_not_called()

    def test_their_devices_are_told_and_their_org_sockets_closed(self):
        self._org_post(self.owner, f"members/{self.coach_member.id}/remove")

        calls = {call.args[0]: call.args[1] for call in self.realtime.call_args_list}
        personal = calls[f"user_notifications_{self.coach.id}"]
        self.assertEqual(personal["notification_type"], "org_membership_changed")
        self.assertEqual(personal["status"], "removed")
        self.assertEqual(personal["role"], "coach")

        sockets = calls[f"org_actor_sockets_{self.org.id}"]
        self.assertEqual(sockets["type"], "org_member_changed")
        self.assertEqual(sockets["user_id"], str(self.coach.id))
        self.assertTrue(sockets["close"])

    def test_cannot_remove_yourself(self):
        self._assert_error(
            self._org_post(self.owner, f"members/{self.owner_member.id}/remove"),
            "cannot_remove_self",
        )

    def test_only_managers_remove(self):
        self._assert_error(
            self._org_post(self.coach, f"members/{self.staff_member.id}/remove"),
            "forbidden",
            403,
        )

    def test_never_the_last_working_owner(self):
        departing = make_user("departing")
        self._member(departing, OrganizationMember.Role.OWNER)
        departing.deletion_requested_at = timezone.now()
        departing.save(update_fields=["deletion_requested_at"])

        response = self._org_post(departing, f"members/{self.owner_member.id}/remove")

        self._assert_error(response, "last_owner")

    def test_removing_an_owner_tells_the_other_owners_with_a_push(self):
        second = make_user("secondowner", "Second Owner")
        second_member = self._member(second, OrganizationMember.Role.OWNER)
        third = make_user("thirdowner", "Third Owner")
        self._member(third, OrganizationMember.Role.OWNER)

        self._org_post(self.owner, f"members/{second_member.id}/remove")

        self.assertEqual(
            self._notifications(third, Notification.Type.ORG_OWNER_CHANGED).count(), 1
        )
        self.assertFalse(
            self._notifications(self.owner, Notification.Type.ORG_OWNER_CHANGED).exists()
        )
        self.assertEqual(self.push.call_count, 1)


class LeaveTests(MembersTestBase):

    def test_any_member_can_leave(self):
        response = self._org_post(self.staff, "members/leave")

        self.assertEqual(response.status_code, 200, response.data)
        member = OrganizationMember.all_objects.get(id=self.staff_member.id)
        self.assertEqual(member.status, OrganizationMember.Status.LEFT)
        self.assertIsNone(member.removed_by)

        calls = {call.args[0]: call.args[1] for call in self.realtime.call_args_list}
        self.assertEqual(calls[f"user_notifications_{self.staff.id}"]["status"], "left")
        self.assertTrue(calls[f"org_actor_sockets_{self.org.id}"]["close"])

    def test_the_last_owner_cannot(self):
        response = self._org_post(self.owner, "members/leave")

        self._assert_error(response, "last_owner")
        self.assertEqual(response.data["message"], "Make someone else an owner first.")

    def test_an_owner_with_a_co_owner_can(self):
        self._member(self.outsider, OrganizationMember.Role.OWNER)

        self.assertEqual(self._org_post(self.owner, "members/leave").status_code, 200)

    def test_a_person_acting_as_themselves_cant(self):
        response = self._user_post(self.owner, "members/leave")
        self.assertEqual(response.status_code, 403)


# ---------------------------------------------------------------------
# A removed member has no access anywhere
# ---------------------------------------------------------------------


class RemovedMemberTests(MembersTestBase):

    def setUp(self):
        super().setUp()
        self._org_post(self.owner, f"members/{self.coach_member.id}/remove")
        self.mail.reset_mock()
        self.push.reset_mock()

    def test_http_acting_as_the_org_is_refused(self):
        response = self.client.get(
            "/organizations/members", **self._as_org(self.coach)
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["message"], "You are not part of this organization")

    def test_gone_from_the_org_list_and_membership_checks(self):
        self.assertFalse(OrganizationService.get_user_organizations(self.coach).exists())
        self.assertEqual(OrganizationService.get_user_memberships(self.coach), {})
        self.assertFalse(OrganizationMemberService.is_organization_member(self.org, self.coach))

        self.client.force_authenticate(user=self.coach)
        self.assertEqual(self.client.get("/organizations/list").data["data"], [])

    def test_the_org_list_joins_on_the_persons_own_row(self):
        # Someone else's active row must not make their removed row count.
        self.assertTrue(
            Organization.objects.filter(
                members__status=OrganizationMember.Status.ACTIVE
            ).filter(members__user=self.coach).exists()
        )
        self.assertNotIn(self.org, OrganizationService.get_user_organizations(self.coach))

    def test_gone_from_every_audience(self):
        OrganizationMember.all_objects.filter(id=self.coach_member.id).update(
            role=OrganizationMember.Role.OWNER
        )

        self.assertEqual(get_org_alert_recipients(self.org), [self.owner])
        self.assertEqual(new_applicant_alert_recipients(self.org), [self.owner.email])
        self.assertNotIn(
            self.coach,
            [
                member.user for member in members_with_permission(
                    self.org, OrgPermission.MEMBERS_MANAGE
                )
            ],
        )

    def test_no_org_push_reaches_them(self):
        notification = Notification.objects.create(
            type=Notification.Type.FOLLOW,
            actor_user=self.outsider,
            recipient_org=self.org,
        )

        from apps.notifications.services.notification_service import _get_recipient_users

        self.assertEqual(_get_recipient_users(notification), [self.owner])

    def test_a_held_publication_is_no_longer_theirs_to_publish(self):
        row = type(
            "Row",
            (),
            {
                "owner_user_id": None,
                "owner_org": self.org,
                "created_by": self.coach,
            },
        )

        self.assertIsNone(PublicationService._rebuild_actor(row))

    def test_a_former_owner_owns_nothing_at_account_deletion(self):
        OrganizationMember.all_objects.filter(id=self.coach_member.id).update(
            role=OrganizationMember.Role.OWNER
        )

        self.assertEqual(sole_owned_organizations(self.coach), [])
        self.assertEqual(sole_owned_organizations(self.owner), [self.org])

    def test_history_still_names_them(self):
        sport = Sport.objects.create(name="Testball", icon_name="mdi:soccer")
        recruitment = Recruitment.objects.create(
            organization=self.org,
            sport=sport,
            title="Open Trial",
            recruitment_type="open_trial",
            status=Recruitment.Status.ACTIVE,
            visibility=Recruitment.Visibility.PUBLIC,
        )
        application = RecruitmentApplication.objects.create(
            recruitment=recruitment,
            applicant=self.outsider,
            shared_name="Otto",
            shared_phone="9999999999",
            status="applied",
        )
        history = RecruitmentApplicationStatusHistory.objects.create(
            application=application,
            from_status="applied",
            to_status="reviewing",
            changed_by_id=self.coach_member.id,
        )

        history = RecruitmentApplicationStatusHistory.objects.get(id=history.id)
        changed_by = ApplicationStatusHistorySerializer(history).data["changed_by"]

        self.assertEqual(changed_by["id"], str(self.coach_member.id))
        self.assertEqual(changed_by["name"], "Cora Coach")
        self.assertTrue(changed_by["is_former"])

        kept = RecruitmentApplicationStatusHistory.objects.create(
            application=application,
            from_status="reviewing",
            to_status="selected",
            changed_by=self.owner_member,
        )
        self.assertFalse(
            ApplicationStatusHistorySerializer(kept).data["changed_by"]["is_former"]
        )

    def test_they_can_be_invited_back(self):
        self.assertEqual(self._invite(user_id=str(self.coach.id)).status_code, 201)


# ---------------------------------------------------------------------
# What the list and the push say
# ---------------------------------------------------------------------


class NotificationCopyTests(MembersTestBase):

    def _row(self, notification):
        rows = NotificationGroupingService.group_notifications([notification])
        return rows[0]

    def test_an_invite_reads_with_its_role_and_opens_the_notifications_page(self):
        self._invite(user_id=str(self.outsider.id))
        notification = self._notifications(self.outsider, Notification.Type.ORG_INVITE).get()

        payload = build_notification_payload(notification)
        self.assertEqual(payload["title"], "Olive Owner invited you to join Dream FC")
        self.assertEqual(payload["body"], "as Coach")
        self.assertEqual(payload["url"], "/notifications")

        row = self._row(notification)
        self.assertEqual(row["text"], "Olive Owner invited you to join Dream FC as Coach")
        self.assertEqual(row["actors"][0]["avatar"], "https://media.example.com/logo.webp")
        self.assertIn("invite_id", row["data"])

    def test_the_other_types(self):
        members_url = f"/organization/admin/{self.org.id}/members"

        self._org_post(self.owner, f"members/{self.coach_member.id}/role", {"role": "staff"})
        changed = self._notifications(self.coach, Notification.Type.ORG_ROLE_CHANGED).get()
        self.assertEqual(
            build_notification_payload(changed)["title"], "You're now Staff at Dream FC"
        )
        self.assertEqual(build_notification_url(changed), members_url)

        self._org_post(self.owner, f"members/{self.coach_member.id}/remove")
        removed = self._notifications(self.coach, Notification.Type.ORG_MEMBER_REMOVED).get()
        self.assertEqual(self._row(removed)["text"], "You were removed from Dream FC")
        self.assertEqual(build_notification_url(removed), "/home")

        invite = self._pending_invite(self.outsider)
        self._user_post(self.outsider, f"invites/{invite.id}/accept")
        joined = self._notifications(self.owner, Notification.Type.ORG_INVITE_ACCEPTED).get()
        self.assertEqual(
            build_notification_payload(joined)["title"], "Otto Outsider joined Dream FC"
        )
        self.assertEqual(build_notification_url(joined), members_url)


# ---------------------------------------------------------------------
# Sockets
# ---------------------------------------------------------------------


@override_settings(
    CHANNEL_LAYERS=CHANNEL_LAYERS,
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class MemberSocketTests(APITransactionTestCase):

    def setUp(self):
        cache.clear()
        self.owner = make_user("clubowner", "Olive Owner")
        self.coach = make_user("clubcoach", "Cora Coach")
        self.org = Organization.objects.create(
            name="Dream FC", username="dreamfc", type=Organization.Type.CLUB
        )
        self.owner_member = OrganizationMember.objects.create(
            organization=self.org, user=self.owner, role=OrganizationMember.Role.OWNER
        )
        self.coach_member = OrganizationMember.objects.create(
            organization=self.org, user=self.coach, role=OrganizationMember.Role.COACH
        )

    def _connect_through_auth(self, user):
        async def scenario():
            communicator = WebsocketCommunicator(
                JWTProtocolAuthMiddleware(UserNotificationsConsumer.as_asgi()),
                f"/ws/notifications/?actor_type=organization&org_id={self.org.id}",
                subprotocols=["access_token", str(AccessToken.for_user(user))],
            )
            connected, _ = await communicator.connect()
            await communicator.disconnect()
            return connected

        return async_to_sync(scenario)()

    def test_connect_is_refused_once_removed(self):
        self.assertTrue(self._connect_through_auth(self.coach))

        OrganizationMember.objects.filter(id=self.coach_member.id).update(
            status=OrganizationMember.Status.REMOVED
        )

        self.assertFalse(self._connect_through_auth(self.coach))

    def _notifications_socket(self, member):
        communicator = WebsocketCommunicator(
            UserNotificationsConsumer.as_asgi(), "/ws/notifications/"
        )
        communicator.scope["user"] = member.user
        communicator.scope["actor"] = Actor(
            actor_type="organization",
            organization=self.org,
            organization_member=member,
        )
        return communicator

    def test_a_removal_tells_and_closes_only_that_members_org_sockets(self):
        async def scenario():
            removed = self._notifications_socket(self.coach_member)
            staying = self._notifications_socket(self.owner_member)
            await removed.connect()
            await staying.connect()

            await sync_to_async(_announce_membership)(
                self.coach.id, self.org.id, "removed", "coach", close=True
            )

            frame = json.loads(await removed.receive_from(timeout=5))
            closed = await removed.receive_output(timeout=5)
            untouched = await staying.receive_nothing(timeout=0.2)

            await staying.disconnect()
            return frame, closed, untouched

        frame, closed, untouched = async_to_sync(scenario)()

        self.assertEqual(
            frame,
            {
                "type": "notification_message",
                "notification_type": "org_membership_changed",
                "organization_id": str(self.org.id),
                "status": "removed",
                "role": "coach",
            },
        )
        self.assertEqual(closed["type"], "websocket.close")
        self.assertEqual(closed["code"], WS_CLOSE_ORG_ACCESS_REVOKED)
        self.assertTrue(untouched)

    def test_a_role_change_reaches_an_org_socket_and_leaves_it_open(self):
        async def scenario():
            socket = self._notifications_socket(self.coach_member)
            await socket.connect()

            await sync_to_async(_announce_membership)(
                self.coach.id, self.org.id, "role_changed", "staff"
            )

            frame = json.loads(await socket.receive_from(timeout=5))
            still_open = await socket.receive_nothing(timeout=0.2)
            await socket.disconnect()
            return frame, still_open

        frame, still_open = async_to_sync(scenario)()

        self.assertEqual(frame["status"], "role_changed")
        self.assertTrue(still_open)

    def _chat_socket(self, conversation):
        communicator = WebsocketCommunicator(
            ChatConsumer.as_asgi(), f"/ws/chat/{conversation.id}/"
        )
        communicator.scope["user"] = self.coach
        communicator.scope["actor"] = Actor(
            actor_type="organization",
            organization=self.org,
            organization_member=self.coach_member,
        )
        communicator.scope["url_route"] = {
            "kwargs": {"conversation_id": str(conversation.id)}
        }
        return communicator

    def _org_conversation(self):
        conversation = Conversation.objects.create(type=Conversation.Type.DIRECT)
        ConversationParticipant.objects.create(
            conversation=conversation, org=self.org, has_accepted=True
        )
        return conversation

    def test_a_removal_closes_their_chat_socket(self):
        conversation = self._org_conversation()

        async def scenario():
            socket = self._chat_socket(conversation)
            connected, _ = await socket.connect()

            await sync_to_async(_announce_membership)(
                self.coach.id, self.org.id, "removed", "coach", close=True
            )

            return connected, await socket.receive_output(timeout=5)

        connected, closed = async_to_sync(scenario)()

        self.assertTrue(connected)
        self.assertEqual(closed["type"], "websocket.close")
        self.assertEqual(closed["code"], WS_CLOSE_ORG_ACCESS_REVOKED)

    def test_a_lost_close_still_stops_them_speaking_for_the_club(self):
        conversation = self._org_conversation()

        async def scenario():
            socket = self._chat_socket(conversation)
            await socket.connect()

            # The channel-layer send that should have closed this was lost.
            await sync_to_async(
                OrganizationMember.objects.filter(id=self.coach_member.id).update
            )(status=OrganizationMember.Status.REMOVED)

            await socket.send_to(text_data=json.dumps({"message": "still here"}))
            return await socket.receive_output(timeout=5)

        closed = async_to_sync(scenario)()

        self.assertEqual(closed["type"], "websocket.close")
        self.assertEqual(closed["code"], WS_CLOSE_ORG_ACCESS_REVOKED)
        self.assertFalse(conversation.messages.exists())
