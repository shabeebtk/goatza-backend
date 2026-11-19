"""
Shared fixtures for the org page and enquiry tests.

An academy (owner, coach, staff) with Football as its sport and one branch,
and three adult players with photos. Everything goes through the real views
and services; only the transports are mocked — the push hand-off
(``notification_service.enqueue``), the storage delete
(``facility_service.enqueue``) and the email sender
(``transactional_emails.send_email_async``).
"""

import datetime
import io
import uuid
from unittest.mock import patch

from django.core.cache import cache
from django.core.management import call_command
from django.test import override_settings
from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.careers.models import CareerEntry
from apps.moderation.services.block_services import BlockService
from apps.organization.models import (
    Organization,
    OrganizationLocation,
    OrganizationMember,
    OrganizationProfile,
    OrganizationSport,
)
from apps.orgpages.models import Program
from apps.sports.models import Sport, SportPosition
from apps.squads.tests.base import MEDIA, child_birthdate, make_user, org_actor
from core.actor import Actor

__all__ = [
    "MEDIA", "PageTestBase", "child_birthdate", "make_user", "org_actor",
    "program_body",
]

VERIFIED = CareerEntry.VerificationStatus.VERIFIED


def program_body(sport, **overrides):
    body = {
        "name": "U12 Football",
        "sport_id": str(sport.id),
        "age_min": 8,
        "age_max": 12,
        "level": "beginner",
        "days": ["sat", "mon"],
        "start_time": "16:30",
        "end_time": "18:00",
        "fee_mode": "amount",
        "fee_amount": 1500,
        "fee_per": "month",
    }
    body.update(overrides)
    return body


@override_settings(
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
    MEDIA_PUBLIC_BASE_URL=MEDIA,
    FRONTEND_BASE_URL="https://goatza.test",
)
class PageTestBase(APITestCase):

    org_type = Organization.Type.ACADEMY

    def setUp(self):
        cache.clear()

        self.football = Sport.objects.create(name="Football", icon_name="mdi:soccer")
        self.cricket = Sport.objects.create(name="Cricket", icon_name="mdi:cricket")
        self.striker = SportPosition.objects.create(
            sport=self.football, name="Striker", group="Forwards"
        )

        self.owner = make_user("pageowner", "Olive Owner", role=User.Role.COACH)
        self.coach = make_user("pagecoach", "Cora Coach", role=User.Role.COACH)
        self.staff = make_user("pagestaff", "Sam Staff", role=User.Role.COACH)

        self.org = self.make_org("Bright Academy", "brightacademy", self.org_type)
        self.owner_member = self._member(self.owner, OrganizationMember.Role.OWNER)
        self.coach_member = self._member(self.coach, OrganizationMember.Role.COACH)
        self.staff_member = self._member(self.staff, OrganizationMember.Role.STAFF)
        OrganizationSport.objects.create(
            organization=self.org, sport=self.football, is_primary=True
        )
        self.branch = OrganizationLocation.objects.create(
            organization=self.org, name="Main Ground", city="Kochi",
            country_code="IN", is_primary=True,
        )

        self.p1 = make_user("asha", "Asha Player")
        self.p2 = make_user("bilal", "Bilal Player")
        self.p3 = make_user("chen", "Chen Player")

        self.push = self._patch("apps.notifications.services.notification_service.enqueue")
        self.storage = self._patch("apps.orgpages.services.facility_service.enqueue")
        self.emails = self._patch("utils.transactional_emails.send_email_async")

    # -----------------------------------------------------------------
    # fixtures
    # -----------------------------------------------------------------
    def _patch(self, target, **kwargs):
        patcher = patch(target, **kwargs)
        mocked = patcher.start()
        self.addCleanup(patcher.stop)
        return mocked

    def make_org(self, name, username, org_type=Organization.Type.ACADEMY, **profile):
        org = Organization.objects.create(name=name, username=username, type=org_type)
        profile.setdefault("logo", f"{MEDIA}/logo.webp")
        OrganizationProfile.objects.create(organization=org, **profile)
        return org

    def _member(self, user, role, org=None):
        return OrganizationMember.objects.create(
            organization=org or self.org, user=user, role=role
        )

    def seed_catalog(self):
        call_command("seed_facility_catalog", stdout=io.StringIO())

    def make_program(self, name="U10", org=None, **fields):
        fields.setdefault("sport", self.football)
        fields.setdefault("age_min", 8)
        fields.setdefault("age_max", 12)
        fields.setdefault("level", Program.Level.BEGINNER)
        fields.setdefault("days", ["mon"])
        fields.setdefault("start_time", datetime.time(16, 0))
        fields.setdefault("end_time", datetime.time(17, 0))
        fields.setdefault("fee_mode", Program.FeeMode.CONTACT)
        return Program.objects.create(organization=org or self.org, name=name, **fields)

    def verified_entry(self, user, org=None, start=datetime.date(2019, 6, 1),
                       end=datetime.date(2023, 5, 1), current=False, **fields):
        fields.setdefault("verification_status", VERIFIED)
        return CareerEntry.objects.create(
            user=user,
            organization=org or self.org,
            sport=self.football,
            title="Player",
            start_date=start,
            end_date=None if current else end,
            is_current=current,
            **fields,
        )

    def page_key(self):
        return f"organizations/{self.org.id}/page/{uuid.uuid4()}.webp"

    def photo_body(self, key=None):
        key = key or self.page_key()
        return {"photo_url": f"{MEDIA}/{key}", "photo_public_id": key}

    def block(self, blocker, target_user=None, target_org=None):
        if isinstance(blocker, User):
            actor = Actor(actor_type="user", user=blocker)
        else:
            owner = OrganizationMember.objects.get(
                organization=blocker, role=OrganizationMember.Role.OWNER
            )
            actor = org_actor(owner)
        BlockService.block(actor, target_user=target_user, target_org=target_org)

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
        return self.client.get(f"/page/{path}", params, **self._org_headers(user, org))

    def _org_write(self, method, user, path, data, org):
        with self.captureOnCommitCallbacks(execute=True):
            return getattr(self.client, method)(
                f"/page/{path}", data if data is not None else {}, format="json",
                **self._org_headers(user, org),
            )

    def org_post(self, user, path, data=None, org=None):
        return self._org_write("post", user, path, data, org)

    def org_patch(self, user, path, data=None, org=None):
        return self._org_write("patch", user, path, data, org)

    def org_put(self, user, path, data=None, org=None):
        return self._org_write("put", user, path, data, org)

    def public_page(self, username=None):
        self.client.force_authenticate(user=None)
        return self.client.get(f"/public/organization/{username or self.org.username}/page")

    def showcase(self, user, username=None):
        self.client.force_authenticate(user=user)
        return self.client.get(f"/page/showcase/{username or self.org.username}")

    def assert_error(self, response, code, status_code=400, field=None):
        self.assertEqual(response.status_code, status_code, response.data)
        self.assertFalse(response.data["success"])
        self.assertEqual(response.data["data"]["code"], code, response.data)
        if field:
            self.assertIn(field, response.data["data"]["errors"], response.data)
