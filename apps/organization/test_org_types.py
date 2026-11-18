"""
An organization is a club, a team or an academy. The "school" type is retired:
migration 0015 turns every school page into a team, and the API refuses
"school" like any other unknown choice, on create and on update.
"""

from importlib import import_module

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase
from rest_framework.test import APITestCase

from apps.organization.models import Organization
from apps.organization.test_create import CreateTestBase, make_org, make_person

retire_school = import_module("apps.organization.migrations.0015_retire_school_type")

UPDATE_URL = "/organizations/update"


class TypeChoicesTests(TestCase):
    def test_the_types_are_club_team_and_academy(self):
        self.assertEqual(
            [value for value, _ in Organization.Type.choices],
            ["club", "team", "academy"],
        )


class SchoolToTeamMigrationTests(TestCase):
    def setUp(self):
        owner = make_person("owner@example.com", "ownerhandle")
        # Written straight to the column: the current model no longer lists
        # "school", exactly like a row saved before this migration.
        self.school = Organization.objects.create(
            name="St. Xavier's College", username="stxaviers", type="school", created_by=owner
        )
        self.club = Organization.objects.create(
            name="Kannur FC", username="kannurfc", type="club", created_by=owner
        )
        self.academy = Organization.objects.create(
            name="Kochi Academy", username="kochiacademy", type="academy", created_by=owner
        )

    def run_data_step(self):
        # The historical models the migration itself is handed, as of 0014.
        state = MigrationExecutor(connection).loader.project_state(
            ("organization", "0014_retire_admin_role")
        )
        retire_school.school_to_team(state.apps, None)

    def test_a_school_becomes_a_team(self):
        self.run_data_step()

        self.school.refresh_from_db()
        self.assertEqual(self.school.type, Organization.Type.TEAM)
        self.assertFalse(Organization.objects.filter(type="school").exists())

    def test_every_other_type_is_left_alone(self):
        self.run_data_step()

        self.club.refresh_from_db()
        self.academy.refresh_from_db()
        self.assertEqual(self.club.type, Organization.Type.CLUB)
        self.assertEqual(self.academy.type, Organization.Type.ACADEMY)

    def test_the_rows_move_before_the_choices_change(self):
        operations = retire_school.Migration.operations

        self.assertEqual(len(operations), 2)
        self.assertEqual(operations[0].code, retire_school.school_to_team)
        self.assertEqual(operations[1].name, "type")


class CreateRefusesSchoolTests(CreateTestBase):
    def test_creating_a_school_is_a_400_on_type(self):
        response = self.create(username="stxaviers", type="school")

        self.assert_refused(response, "invalid")
        self.assertIn("type", response.data["data"]["errors"])
        self.assertEqual(response.data["data"]["field"], "type")
        self.assertFalse(Organization.objects.exists())

    def test_a_school_or_college_team_is_created_as_a_team(self):
        response = self.create(name="St. Xavier's College", username="stxaviers", type="team")

        self.assertEqual(response.status_code, 201, response.data)
        org = Organization.objects.get(id=response.data["data"]["id"])
        self.assertEqual(org.type, Organization.Type.TEAM)


class UpdateRefusesSchoolTests(APITestCase):
    def setUp(self):
        self.owner = make_person("owner@example.com", "ownerhandle")
        self.org = make_org(self.owner, "heldfc")
        self.client.force_authenticate(self.owner)

    def update(self, body):
        return self.client.patch(f"{UPDATE_URL}?org_id={self.org.id}", body, format="json")

    def test_updating_to_school_is_a_400_on_type(self):
        response = self.update({"type": "school"})

        self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(response.data["success"])
        self.assertIn("type", response.data["data"])
        self.org.refresh_from_db()
        self.assertEqual(self.org.type, Organization.Type.CLUB)

    def test_updating_to_team_is_accepted(self):
        response = self.update({"type": "team"})

        self.assertEqual(response.status_code, 200, response.data)
        self.org.refresh_from_db()
        self.assertEqual(self.org.type, Organization.Type.TEAM)
