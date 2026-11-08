"""
Creating a page: the chosen handle, the 3-page limit and its eligibility
endpoint, the codes the create answers with, the availability check a page's
handle goes through (`for=organization`), and attaching the first logo while
still acting as the person.
"""

from unittest.mock import patch

from django.core.cache import cache
from django.test import override_settings
from rest_framework.test import APITestCase

from apps.accounts.models import User, UserProfile
from apps.guardians.testing import grant_guardian_consent
from apps.legal.testing import accept_current_terms
from apps.organization.models import (
    Organization,
    OrganizationMember,
    OrganizationProfile,
    OrganizationSport,
)
from apps.organization.services.organization_service import (
    ORG_LIMIT_MESSAGE,
    OrganizationService,
)
from apps.sports.models import Sport
from apps.usernames.models import UsernameRegistry
from apps.usernames.services.username_service import UsernameService
from utils.validations import validate_username_format

CREATE_URL = "/organizations/create"
ELIGIBILITY_URL = "/organizations/create/eligibility"
CHECK_URL = "/user/check/username/availability"
MEDIA_URL = "/organizations/update/logo/cover"
MEDIA_BASE = "https://media.example.com"


def make_person(email, handle, name="Person"):
    user = User.objects.create_user(email=email, password="pass1234", is_email_verified=True)
    accept_current_terms(user)
    with patch("utils.transactional_emails.send_email_async"):
        grant_guardian_consent(user)
    UserProfile.objects.create(user=user, name=name)
    UsernameService.claim(handle, user=user)
    user.refresh_from_db()
    return user


def make_org(owner, handle, name="Held FC"):
    org = Organization.objects.create(
        name=name, username=handle, type=Organization.Type.CLUB, created_by=owner
    )
    OrganizationProfile.objects.create(organization=org)
    OrganizationMember.objects.create(
        organization=org, user=owner, role=OrganizationMember.Role.OWNER
    )
    UsernameRegistry.objects.create(username_lower=handle, organization=org)
    return org


class CreateTestBase(APITestCase):
    def setUp(self):
        cache.clear()
        self.person = make_person("maker@example.com", "makerhandle", name="Maker")
        self.other = make_person("other@example.com", "otherhandle", name="Other")
        self.client.force_authenticate(self.person)

    def create(self, **body):
        body.setdefault("name", "Kannur FC")
        body.setdefault("type", "club")
        return self.client.post(CREATE_URL, body, format="json")

    def assert_refused(self, response, code, status_code=400):
        self.assertEqual(response.status_code, status_code, response.data)
        self.assertFalse(response.data["success"])
        self.assertEqual(response.data["data"]["code"], code)
        self.assertTrue(response.data["data"]["message"])
        self.assertEqual(response.data["message"], response.data["data"]["message"])


class CreateWithHandleTests(CreateTestBase):
    def test_creates_with_the_chosen_handle(self):
        response = self.create(username="kannur_fc")

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["data"]["username"], "kannur_fc")
        org = Organization.objects.get(id=response.data["data"]["id"])
        self.assertEqual(org.username, "kannur_fc")
        self.assertEqual(org.created_by, self.person)
        self.assertTrue(
            UsernameRegistry.objects.filter(username_lower="kannur_fc", organization=org).exists()
        )
        self.assertTrue(
            OrganizationMember.objects.filter(
                organization=org, user=self.person, role=OrganizationMember.Role.OWNER
            ).exists()
        )

    def test_the_handle_is_normalized(self):
        response = self.create(username="  Kannur_FC  ")

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["data"]["username"], "kannur_fc")

    def test_without_a_handle_one_is_generated(self):
        response = self.create(name="Kochi Academy")

        self.assertEqual(response.status_code, 201, response.data)
        handle = response.data["data"]["username"]
        self.assertTrue(handle.startswith("kochiacademy"))
        self.assertEqual(validate_username_format(handle), handle)

    def test_a_blank_handle_means_generate_one(self):
        response = self.create(name="Kochi Academy", username="")

        self.assertEqual(response.status_code, 201, response.data)
        self.assertTrue(response.data["data"]["username"].startswith("kochiacademy"))

    def test_a_handle_another_person_holds_is_taken(self):
        response = self.create(username="otherhandle")

        self.assert_refused(response, "username_taken")
        self.assertEqual(response.data["data"]["field"], "username")
        self.assertFalse(Organization.objects.filter(created_by=self.person).exists())

    def test_your_own_handle_is_taken_for_your_page(self):
        response = self.create(username="makerhandle")

        self.assert_refused(response, "username_taken")
        self.assertEqual(response.data["data"]["field"], "username")
        self.assertFalse(Organization.objects.filter(created_by=self.person).exists())

    def test_a_handle_an_org_holds_is_taken(self):
        make_org(self.other, "heldbyorg")

        response = self.create(username="HeldByOrg")

        self.assert_refused(response, "username_taken")

    def test_an_invalid_handle_is_refused_with_its_reason(self):
        response = self.create(username="kannur.fc")

        self.assert_refused(response, "username_invalid")
        self.assertEqual(response.data["data"]["field"], "username")
        self.assertEqual(
            response.data["data"]["message"], "Only letters, numbers, and underscores allowed"
        )
        self.assertFalse(Organization.objects.filter(created_by=self.person).exists())

    def test_a_reserved_handle_is_invalid(self):
        self.assert_refused(self.create(username="settings"), "username_invalid")

    def test_a_taken_handle_rolls_the_whole_create_back(self):
        sport = Sport.objects.create(name="Football", icon_name="mdi:soccer")

        response = self.create(username="otherhandle", sport_ids=[str(sport.id)])

        self.assert_refused(response, "username_taken")
        self.assertEqual(Organization.objects.count(), 0)
        self.assertEqual(OrganizationSport.objects.count(), 0)


class CreateValidationTests(CreateTestBase):
    def test_description_over_600_is_refused(self):
        response = self.create(username="kannur_fc", description="x" * 601)

        self.assert_refused(response, "invalid")
        self.assertIn("description", response.data["data"]["errors"])
        self.assertEqual(response.data["data"]["field"], "description")
        self.assertFalse(Organization.objects.exists())

    def test_description_of_600_is_fine(self):
        response = self.create(username="kannur_fc", description="x" * 600)

        self.assertEqual(response.status_code, 201, response.data)
        org = Organization.objects.get(id=response.data["data"]["id"])
        self.assertEqual(len(org.profile.description), 600)

    def test_sports_keep_their_order_and_the_first_is_primary(self):
        cricket = Sport.objects.create(name="Cricket", icon_name="mdi:cricket")
        athletics = Sport.objects.create(name="Athletics", icon_name="mdi:run")

        # Athletics sorts first by name; the page picked Cricket first.
        response = self.create(
            username="kannur_fc", sport_ids=[str(cricket.id), str(athletics.id)]
        )

        self.assertEqual(response.status_code, 201, response.data)
        rows = OrganizationSport.objects.filter(organization_id=response.data["data"]["id"])
        primary = rows.get(is_primary=True)
        self.assertEqual(primary.sport, cricket)
        self.assertEqual(rows.count(), 2)


class LimitTests(CreateTestBase):
    def _fill_up(self):
        for index in range(OrganizationService.MAX_ORG_PER_USER):
            make_org(self.person, f"mine_{index}", name=f"Mine {index}")

    def test_the_fourth_page_is_refused_with_the_limit_code(self):
        self._fill_up()

        response = self.create(username="one_too_many")

        self.assert_refused(response, "org_limit")
        self.assertEqual(response.data["data"]["message"], ORG_LIMIT_MESSAGE)
        self.assertEqual(ORG_LIMIT_MESSAGE, "You can create up to 3 pages.")
        self.assertFalse(Organization.objects.filter(username="one_too_many").exists())

    def test_the_limit_counts_pages_created_not_pages_joined(self):
        for index in range(3):
            org = make_org(self.other, f"theirs_{index}")
            OrganizationMember.objects.create(
                organization=org, user=self.person, role=OrganizationMember.Role.COACH
            )

        self.assertEqual(self.create(username="my_first").status_code, 201)

    def test_eligibility_with_room_left(self):
        make_org(self.person, "mine_0")

        response = self.client.get(ELIGIBILITY_URL)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"], {"allowed": True, "limit": 3, "used": 1})

    def test_eligibility_at_the_limit(self):
        self._fill_up()

        response = self.client.get(ELIGIBILITY_URL)

        self.assertEqual(response.data["data"], {"allowed": False, "limit": 3, "used": 3})

    def test_eligibility_needs_a_login(self):
        self.client.force_authenticate(None)

        self.assertEqual(self.client.get(ELIGIBILITY_URL).status_code, 401)

    def test_the_count_is_taken_under_a_lock_on_the_person(self):
        with patch.object(
            User.objects, "select_for_update", wraps=User.objects.select_for_update
        ) as lock:
            self.assertEqual(self.create(username="locked_in").status_code, 201)

        lock.assert_called_once()


class OrgHandleAvailabilityTests(CreateTestBase):
    def setUp(self):
        super().setUp()
        self.my_org = make_org(self.person, "myclub", name="My Club")
        self.their_org = make_org(self.other, "theirclub", name="Their Club")

    def check(self, username, **params):
        return self.client.get(CHECK_URL, {"username": username, **params})

    def test_your_own_handle_is_not_available_for_a_page(self):
        response = self.check("makerhandle", **{"for": "organization"})

        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["data"]["valid"])
        self.assertFalse(response.data["data"]["available"])

    def test_a_free_handle_is_available_and_carries_no_suggestion(self):
        response = self.check("brand_new_club", **{"for": "organization"})

        self.assertTrue(response.data["data"]["available"])
        self.assertNotIn("suggestion", response.data["data"])

    def test_a_taken_handle_comes_with_a_free_suggestion(self):
        response = self.check("theirclub", **{"for": "organization"})

        data = response.data["data"]
        self.assertFalse(data["available"])
        suggestion = data["suggestion"]
        self.assertTrue(suggestion.startswith("theirclub"))
        self.assertEqual(validate_username_format(suggestion), suggestion)
        self.assertFalse(UsernameRegistry.objects.filter(username_lower=suggestion).exists())

    def test_the_orgs_own_handle_is_available_to_a_member_renaming_it(self):
        response = self.check(
            "myclub", **{"for": "organization", "org_id": str(self.my_org.id)}
        )

        self.assertTrue(response.data["data"]["available"])

    def test_the_orgs_own_handle_is_not_available_to_a_non_member(self):
        response = self.check(
            "theirclub", **{"for": "organization", "org_id": str(self.their_org.id)}
        )

        self.assertFalse(response.data["data"]["available"])
        self.assertIn("suggestion", response.data["data"])

    def test_a_removed_member_cannot_use_the_orgs_handle_either(self):
        member = OrganizationMember.objects.create(
            organization=self.their_org, user=self.person, role=OrganizationMember.Role.COACH
        )
        member.status = OrganizationMember.Status.REMOVED
        member.save(update_fields=["status"])

        response = self.check(
            "theirclub", **{"for": "organization", "org_id": str(self.their_org.id)}
        )

        self.assertFalse(response.data["data"]["available"])

    def test_org_id_does_not_free_your_own_personal_handle(self):
        response = self.check(
            "makerhandle", **{"for": "organization", "org_id": str(self.my_org.id)}
        )

        self.assertFalse(response.data["data"]["available"])

    def test_a_malformed_org_id_is_ignored(self):
        response = self.check("myclub", **{"for": "organization", "org_id": "not-a-uuid"})

        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(response.data["data"]["available"])

    def test_an_invalid_handle_is_a_400_with_the_reason(self):
        response = self.check("my.club", **{"for": "organization"})

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.data["data"]["valid"])
        self.assertEqual(response.data["message"], "Only letters, numbers, and underscores allowed")


class PersonHandleAvailabilityUnchangedTests(CreateTestBase):
    """Without `for`, the check answers for the person, exactly as before."""

    def setUp(self):
        super().setUp()
        make_org(self.other, "theirclub")

    def test_your_own_handle_is_available_to_you(self):
        response = self.client.get(CHECK_URL, {"username": "makerhandle"})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["data"]["available"])
        self.assertNotIn("suggestion", response.data["data"])

    def test_an_orgs_handle_is_taken_and_no_suggestion_is_added(self):
        response = self.client.get(CHECK_URL, {"username": "theirclub"})

        self.assertFalse(response.data["data"]["available"])
        self.assertNotIn("suggestion", response.data["data"])

    def test_another_persons_handle_is_taken(self):
        response = self.client.get(CHECK_URL, {"username": "otherhandle"})

        self.assertFalse(response.data["data"]["available"])

    def test_an_unknown_for_value_is_the_person_scoped_check(self):
        response = self.client.get(CHECK_URL, {"username": "makerhandle", "for": "user"})

        self.assertTrue(response.data["data"]["available"])


@override_settings(MEDIA_PUBLIC_BASE_URL=MEDIA_BASE)
class AttachFirstLogoAsThePersonTests(CreateTestBase):
    """
    The create page attaches the logo right after the create, still acting as
    the person, naming the org with ?org_id=. The key is checked against THAT
    org, not against the actor's (which is None for a person).
    """

    def setUp(self):
        super().setUp()
        self.org = make_org(self.person, "freshclub", name="Fresh Club")

    def attach(self, org, key, field="logo"):
        return self.client.post(
            f"{MEDIA_URL}?org_id={org.id}",
            {field: f"{MEDIA_BASE}/{key}", f"{field}_public_id": key}
            if field == "logo"
            else {"cover_image": f"{MEDIA_BASE}/{key}", "cover_image_public_id": key},
            format="json",
        )

    def test_a_logo_under_the_orgs_prefix_is_attached(self):
        key = f"organizations/{self.org.id}/logo.webp"

        response = self.attach(self.org, key)

        self.assertEqual(response.status_code, 200, response.data)
        self.org.profile.refresh_from_db()
        self.assertEqual(self.org.profile.logo_public_id, key)
        self.assertTrue(self.org.profile.logo.startswith(f"{MEDIA_BASE}/{key}?v="))
        self.assertEqual(response.data["data"]["logo"], self.org.profile.logo)

    def test_a_cover_under_the_orgs_prefix_is_attached(self):
        key = f"organizations/{self.org.id}/cover.webp"

        response = self.attach(self.org, key, field="cover_image")

        self.assertEqual(response.status_code, 200, response.data)
        self.org.profile.refresh_from_db()
        self.assertEqual(self.org.profile.cover_image_public_id, key)
        self.assertTrue(response.data["data"]["cover_image"].startswith(f"{MEDIA_BASE}/{key}?v="))

    def test_a_key_under_the_persons_own_prefix_is_refused(self):
        key = f"users/{self.person.id}/organization_logo/logo.webp"

        response = self.attach(self.org, key)

        self.assertEqual(response.status_code, 400, response.data)
        self.org.profile.refresh_from_db()
        self.assertEqual(self.org.profile.logo, "")

    def test_another_orgs_prefix_is_refused(self):
        other_org = make_org(self.person, "secondclub", name="Second Club")
        key = f"organizations/{other_org.id}/logo.webp"

        response = self.attach(self.org, key)

        self.assertEqual(response.status_code, 400, response.data)

    def test_a_non_member_cannot_attach(self):
        their_org = make_org(self.other, "theirclub")
        key = f"organizations/{their_org.id}/logo.webp"

        response = self.attach(their_org, key)

        self.assertEqual(response.status_code, 400, response.data)
        their_org.profile.refresh_from_db()
        self.assertEqual(their_org.profile.logo, "")
