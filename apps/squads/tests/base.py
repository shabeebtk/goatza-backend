"""
Shared fixtures for the squads tests.

A club (owner, coach, staff), three adult players with photos, and Football with
its positions grouped and a SportSquadConfig. Everything goes through the real
views and services; only the transports are mocked — the push hand-off
(``notification_service.enqueue``) and the storage delete (``squad_service.enqueue``).
"""

import datetime
from unittest.mock import patch

from django.core.cache import cache
from django.db import transaction
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.accounts.models import User, UserProfile
from apps.guardians.testing import grant_guardian_consent
from apps.legal.testing import accept_current_terms
from apps.moderation.services.block_services import BlockService
from apps.organization.models import Organization, OrganizationMember, OrganizationProfile
from apps.sports.models import Sport, SportPosition, UserSportPosition
from apps.squads.models import Squad, SquadMember, SportSquadConfig
from core.actor import Actor

ADULT = datetime.date(1995, 5, 17)
CHILD_YEARS = 14

MEDIA = "https://media.example.com"


def child_birthdate():
    today = timezone.localdate()
    return today.replace(year=today.year - CHILD_YEARS)


def make_user(username, name=None, role=User.Role.PLAYER, birthdate=ADULT,
              is_public_profile=True, **fields):
    fields.setdefault("is_email_verified", True)
    user = User.objects.create_user(
        email=fields.pop("email", f"{username}@example.com"),
        password="pass1234",
        username=username,
        role=role,
        **fields,
    )
    accept_current_terms(user)
    with patch("utils.transactional_emails.send_email_async"):
        grant_guardian_consent(user)
    UserProfile.objects.create(
        user=user,
        name=name or username.title(),
        birthdate=birthdate,
        profile_photo=f"{MEDIA}/users/{username}.webp",
        is_public_profile=is_public_profile,
    )
    return user


def org_actor(member):
    return Actor(
        actor_type="organization",
        organization=member.organization,
        organization_member=member,
    )


@override_settings(
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
    MEDIA_PUBLIC_BASE_URL=MEDIA,
    FRONTEND_BASE_URL="https://goatza.test",
)
class SquadTestBase(APITestCase):

    def setUp(self):
        cache.clear()

        self.football = Sport.objects.create(name="Football", icon_name="mdi:soccer")
        self.cricket = Sport.objects.create(name="Cricket", icon_name="mdi:cricket")
        self.gk = SportPosition.objects.create(
            sport=self.football, name="Goalkeeper", group="Goalkeepers"
        )
        self.defender = SportPosition.objects.create(
            sport=self.football, name="Defender", group="Defenders"
        )
        self.striker = SportPosition.objects.create(
            sport=self.football, name="Striker", group="Forwards"
        )
        self.batsman = SportPosition.objects.create(
            sport=self.cricket, name="Batsman", group="Batters"
        )
        SportSquadConfig.objects.create(
            sport=self.football,
            uses_jersey_numbers=True,
            group_order=["Goalkeepers", "Defenders", "Midfielders", "Forwards"],
            typical_squad_size=25,
        )

        self.owner = make_user("clubowner", "Olive Owner", role=User.Role.COACH)
        self.coach = make_user("clubcoach", "Cora Coach", role=User.Role.COACH)
        self.staff = make_user("clubstaff", "Sam Staff", role=User.Role.COACH)

        self.org = Organization.objects.create(
            name="Dream FC", username="dreamfc", type=Organization.Type.CLUB
        )
        OrganizationProfile.objects.create(
            organization=self.org, logo=f"{MEDIA}/logo.webp"
        )
        self.owner_member = self._member(self.owner, OrganizationMember.Role.OWNER)
        self.coach_member = self._member(self.coach, OrganizationMember.Role.COACH)
        self.staff_member = self._member(self.staff, OrganizationMember.Role.STAFF)

        self.p1 = make_user("asha", "Asha Player")
        self.p2 = make_user("bilal", "Bilal Player")
        self.p3 = make_user("chen", "Chen Player")

        self.squad = self.make_squad("U17 Boys", age_group="U17", level="youth")

        self.push = self._patch("apps.notifications.services.notification_service.enqueue")
        self.storage = self._patch("apps.squads.services.squad_service.enqueue")

    # -----------------------------------------------------------------
    # fixtures
    # -----------------------------------------------------------------
    def _patch(self, target, **kwargs):
        patcher = patch(target, **kwargs)
        mocked = patcher.start()
        self.addCleanup(patcher.stop)
        return mocked

    def _member(self, user, role, org=None):
        return OrganizationMember.objects.create(
            organization=org or self.org, user=user, role=role
        )

    def make_squad(self, name, org=None, sport=None, **fields):
        return Squad.objects.create(
            organization=org or self.org,
            sport=sport or self.football,
            name=name,
            **fields,
        )

    def activate(self, squad, user, **fields):
        """An ACTIVE row straight in the table — for reads that need no career entry."""
        fields.setdefault("source", SquadMember.Source.INVITE)
        fields.setdefault("joined_at", timezone.localdate())
        row = SquadMember.objects.create(
            squad=squad, user=user, status=SquadMember.Status.ACTIVE, **fields
        )
        Squad.objects.filter(id=squad.id).update(
            members_count=SquadMember.objects.filter(
                squad=squad, status=SquadMember.Status.ACTIVE
            ).count()
        )
        return row

    def primary_position(self, user, position):
        return UserSportPosition.objects.create(
            user=user, sport=position.sport, position=position, is_primary=True
        )

    def block(self, blocker, target_user=None, target_org=None):
        """A user blocks, or the club blocks (as its owner — org.block is owners')."""
        if isinstance(blocker, User):
            actor = Actor(actor_type="user", user=blocker)
        else:
            owner = OrganizationMember.objects.get(
                organization=blocker, role=OrganizationMember.Role.OWNER
            )
            actor = org_actor(owner)
        BlockService.block(actor, target_user=target_user, target_org=target_org)

    def owner_actor(self):
        return org_actor(self.owner_member)

    def coach_actor(self):
        return org_actor(self.coach_member)

    # -----------------------------------------------------------------
    # requests
    # -----------------------------------------------------------------
    def _org_headers(self, user, org=None):
        self.client.force_authenticate(user=user)
        return {
            "HTTP_X_ACTOR_TYPE": "organization",
            "HTTP_X_ACTOR_ID": str((org or self.org).id),
        }

    def org_get(self, user, path, org=None, **params):
        return self.client.get(f"/squads/{path}", params, **self._org_headers(user, org))

    def org_post(self, user, path, data=None, org=None):
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(
                f"/squads/{path}", data or {}, format="json",
                **self._org_headers(user, org),
            )

    def org_patch(self, user, path, data=None, org=None):
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.patch(
                f"/squads/{path}", data or {}, format="json",
                **self._org_headers(user, org),
            )

    def user_get(self, user, path, **params):
        self.client.force_authenticate(user=user)
        return self.client.get(f"/squads/{path}", params)

    def user_post(self, user, path, data=None):
        self.client.force_authenticate(user=user)
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(f"/squads/{path}", data or {}, format="json")

    def anon_get(self, path):
        self.client.force_authenticate(user=None)
        return self.client.get(path)

    def run_after_commit(self, fn, *args, **kwargs):
        """A service call with its on_commit side effects run, like a request."""
        with self.captureOnCommitCallbacks(execute=True):
            return fn(*args, **kwargs)

    def assert_error(self, response, code, status_code=400):
        self.assertEqual(response.status_code, status_code, response.data)
        self.assertFalse(response.data["success"])
        self.assertEqual(response.data["data"]["code"], code, response.data)

    def assert_raises_code(self, code, fn, *args, **kwargs):
        from apps.squads.errors import SquadError

        with self.assertRaises(SquadError) as ctx:
            with transaction.atomic():
                fn(*args, **kwargs)
        self.assertEqual(ctx.exception.code, code, ctx.exception.message)
        return ctx.exception


def find_keys(value, keys):
    """Every key from ``keys`` found anywhere in a nested payload."""
    found = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in keys:
                found.add(key)
            found |= find_keys(item, keys)
    elif isinstance(value, (list, tuple)):
        for item in value:
            found |= find_keys(item, keys)
    return found
