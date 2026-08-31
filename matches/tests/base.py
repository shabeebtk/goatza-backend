"""
Shared cast and helpers for the Match Diary tests.

Tests hit the API where the rule is about HTTP (status codes, envelopes, query
params, what a client can and cannot name in a body) and call the service or the
selectors directly where the rule is about data (the DB constraint, streak
arithmetic, ProtectedError). Both surfaces matter and neither one proves the
other: a serializer that silently drops a field passes every service test.

The catalog is built here rather than by running ``seed_match_stat_fields`` — a
test that depends on the seed's contents starts failing the day somebody adds a
stat, and none of these rules are about football.
"""

from datetime import date, timedelta
from decimal import Decimal

from django.core.cache import cache
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import User, UserProfile
from careers.models import CareerEntry
from core.actor import Actor
from matches.models import MatchEntry, SportMatchStatField
from matches.services.match_services import MatchService
from organization.models import (
    Organization,
    OrganizationMember,
    OrganizationProfile,
)
from sports.models import Sport, SportPosition
from legal.testing import accept_current_terms

BASE_URL = "/matches"
CREATE_URL = f"{BASE_URL}/create"
LIST_URL = f"{BASE_URL}/list"
UPCOMING_URL = f"{BASE_URL}/upcoming"
SUMMARY_URL = f"{BASE_URL}/summary"
SETTINGS_URL = f"{BASE_URL}/settings"
STAT_FIELDS_URL = f"{BASE_URL}/stat-fields"


def update_url(match_id):
    return f"{BASE_URL}/{match_id}/update"


def delete_url(match_id):
    return f"{BASE_URL}/{match_id}"


@override_settings(
    # Five users per test; the real hasher makes setUp dominate the run.
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"]
)
class MatchDiaryTestCase(APITestCase):
    """
    Shared cast: two players, one of every role that must be refused, one org,
    two sports and a small stat catalog covering both value types plus a
    retired row.
    """

    def setUp(self):
        cache.clear()

        self.player = self._user("player", User.Role.PLAYER)
        self.other = self._user("other", User.Role.PLAYER)
        self.coach = self._user("coach", User.Role.COACH)
        self.scout = self._user("scout", User.Role.SCOUT)
        self.orguser = self._user("orguser", User.Role.ORG_USER)

        self.org = self._org("dreamfc", "Dream FC")
        # the org actor is only honored for a verified member
        self.membership = OrganizationMember.objects.create(
            organization=self.org,
            user=self.orguser,
            role=OrganizationMember.Role.OWNER,
        )

        self.football = Sport.objects.create(name="Football", icon_name="mdi:soccer")
        self.cricket = Sport.objects.create(name="Cricket", icon_name="mdi:cricket")

        self.striker = SportPosition.objects.create(
            sport=self.football, name="Striker"
        )
        self.keeper = SportPosition.objects.create(
            sport=self.football, name="Goalkeeper"
        )
        self.batsman = SportPosition.objects.create(
            sport=self.cricket, name="Batsman"
        )

        self.goals = self._stat_field(
            self.football, "Goals", "G", order=1, is_primary=True
        )
        self.assists = self._stat_field(
            self.football, "Assists", "A", order=2, is_primary=True
        )
        self.conceded = self._stat_field(
            self.football, "Goals conceded", "GC", order=3
        )
        self.distance = self._stat_field(
            self.football,
            "Distance covered",
            "DIST",
            order=4,
            value_type=SportMatchStatField.ValueType.DECIMAL,
            unit="km",
        )
        self.retired = self._stat_field(
            self.football, "Offsides", "OFF", order=5, is_active=False
        )
        self.wickets = self._stat_field(self.cricket, "Wickets", "W", order=1)

        self.actor = Actor(actor_type="user", user=self.player)
        self.other_actor = Actor(actor_type="user", user=self.other)
        self.coach_actor = Actor(actor_type="user", user=self.coach)
        self.scout_actor = Actor(actor_type="user", user=self.scout)
        self.org_actor = Actor(
            actor_type="organization",
            organization=self.org,
            organization_member=self.membership,
        )

        self.today = timezone.localdate()
        # The Monday of the current ISO week — every streak test counts from
        # here, so none of them depend on which day the suite happens to run.
        self.monday = self.today - timedelta(days=self.today.weekday())
        # A fixed mid-year day in the previous calendar year, for the ?year
        # filter. Mid-year and fixed on purpose: "today minus 365 days" lands in
        # the same year on the last day of a leap year, and replacing the year
        # on today's date explodes on 29 February.
        self.last_season = date(self.today.year - 1, 6, 15)

    # ── factories ────────────────────────────────────────────────

    def _user(self, username, role):
        user = User.objects.create_user(
            email=f"{username}@example.com",
            password="pass1234",
            username=username,
            role=role,
        )
        accept_current_terms(user)
        UserProfile.objects.create(user=user, name=username.title())
        return user

    def _org(self, username, name):
        org = Organization.objects.create(
            name=name,
            username=username,
            type=Organization.Type.CLUB,
        )
        OrganizationProfile.objects.create(organization=org)
        return org

    def _stat_field(self, sport, name, short_label, **kwargs):
        return SportMatchStatField.objects.create(
            sport=sport,
            name=name,
            short_label=short_label,
            value_type=kwargs.pop(
                "value_type", SportMatchStatField.ValueType.INTEGER
            ),
            **kwargs,
        )

    def _career_entry(self, user=None, **kwargs):
        return CareerEntry.objects.create(
            user=user or self.player,
            organization_name=kwargs.pop("organization_name", "Old Town FC"),
            sport=kwargs.pop("sport", self.football),
            title=kwargs.pop("title", "Player"),
            start_date=kwargs.pop("start_date", date(2024, 1, 1)),
            **kwargs,
        )

    def _played(self, actor=None, **overrides):
        """A played match, straight through the service."""
        payload = {
            "sport": self.football.id,
            "date": self.today,
            "result": MatchEntry.Result.WIN,
            "minutes_played": 90,
        }
        payload.update(overrides)
        return MatchService.create_match(actor or self.actor, **payload)

    def _fixture(self, actor=None, **overrides):
        """A scheduled fixture, straight through the service."""
        payload = {
            "sport": self.football.id,
            "date": self.today + timedelta(days=3),
            "status": MatchEntry.Status.SCHEDULED,
            "opponent_name": "Riverside FC",
        }
        payload.update(overrides)
        return MatchService.create_match(actor or self.actor, **payload)

    def _stat_payload(self, *pairs):
        """``(field, value)`` pairs → the wire shape a write body carries."""
        return [
            {"stat_field_id": str(field.id), "value": str(value)}
            for field, value in pairs
        ]

    # ── request helpers ──────────────────────────────────────────

    def _auth(self, user, org=None):
        self.client.force_authenticate(user=user)
        if org is None:
            return {}
        return {
            "HTTP_X_ACTOR_TYPE": "organization",
            "HTTP_X_ACTOR_ID": str(org.id),
        }

    def _create(self, user=None, body=None, org=None):
        headers = self._auth(user or self.player, org)
        payload = {
            "sport": str(self.football.id),
            "date": str(self.today),
        }
        payload.update(body or {})
        return self.client.post(CREATE_URL, payload, format="json", **headers)

    def _update(self, match_id, body, user=None, org=None):
        headers = self._auth(user or self.player, org)
        return self.client.patch(
            update_url(match_id), body, format="json", **headers
        )

    def _delete(self, match_id, user=None, org=None):
        headers = self._auth(user or self.player, org)
        return self.client.delete(delete_url(match_id), **headers)

    def _list(self, user=None, org=None, **params):
        headers = self._auth(user or self.player, org)
        return self.client.get(LIST_URL, params, **headers)

    def _upcoming(self, user=None, org=None, **params):
        headers = self._auth(user or self.player, org)
        return self.client.get(UPCOMING_URL, params, **headers)

    def _summary(self, user=None, org=None, **params):
        headers = self._auth(user or self.player, org)
        return self.client.get(SUMMARY_URL, params, **headers)

    def _get_settings(self, user=None, org=None):
        headers = self._auth(user or self.player, org)
        return self.client.get(SETTINGS_URL, **headers)

    def _patch_settings(self, body, user=None, org=None):
        headers = self._auth(user or self.player, org)
        return self.client.patch(SETTINGS_URL, body, format="json", **headers)

    def _stat_fields(self, user=None, org=None, **params):
        headers = self._auth(user or self.player, org)
        return self.client.get(STAT_FIELDS_URL, params, **headers)

    # ── assertions ───────────────────────────────────────────────

    def _results(self, resp):
        return resp.data["data"]["results"]

    def _ids(self, resp):
        return [row["id"] for row in self._results(resp)]

    def _stats_by_label(self, resp_row):
        """
        ``{short_label: Decimal}`` for one serialized match.

        Normalized through Decimal rather than compared as-is: DRF's
        COERCE_DECIMAL_TO_STRING renders decimals as strings by default, and a
        test that hardcoded "2.00" would start failing the day somebody flips
        that setting without anything actually breaking.
        """
        return {
            row["short_label"]: Decimal(str(row["value"]))
            for row in resp_row["stats"]
        }

    def _summary_stats(self, resp):
        """``{short_label: row}`` from a summary response."""
        return {row["short_label"]: row for row in resp.data["data"]["stats"]}
