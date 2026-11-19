"""The page as everyone else sees it — one selector, two surfaces."""

from apps.accounts.models import UserProfile
from apps.organization.models import Organization, OrganizationProfile
from apps.orgpages.models import PageSettings
from apps.orgpages.tests.base import PageTestBase, program_body
from apps.squads.tests.base import find_keys


class PublicPageTests(PageTestBase):

    def test_the_shape_and_the_academy_order(self):
        data = self.public_page().data["data"]
        self.assertEqual(
            data["organization"],
            {"id": str(self.org.id), "username": "brightacademy",
             "name": "Bright Academy", "type": "academy"},
        )
        self.assertEqual(
            data["order"],
            ["about", "programs", "facilities", "coaches", "players",
             "squads", "locations", "trials", "posts"],
        )
        self.assertIsNone(data["facts"])
        self.assertEqual(data["programs"], [])
        self.assertEqual(data["facilities"], [])
        self.assertEqual(
            data["contact"],
            {"enquiry_form": True, "whatsapp": None, "call": None, "email": None},
        )

    def test_a_club_orders_its_sections_differently(self):
        self.make_org("Dream FC", "dreamfc", Organization.Type.CLUB)
        data = self.public_page("dreamfc").data["data"]
        self.assertEqual(data["order"][:3], ["about", "squads", "trials"])
        self.assertEqual(data["order"][-1], "posts")
        self.assertFalse(data["contact"]["enquiry_form"])

    def test_a_switched_off_section_is_out_of_order_and_empty(self):
        self.org_post(self.owner, "programs", program_body(self.football))
        self.org_patch(self.owner, "sections", {"programs": False, "squads": False})

        data = self.public_page().data["data"]
        self.assertNotIn("programs", data["order"])
        self.assertNotIn("squads", data["order"])
        self.assertEqual(data["order"][-1], "posts")
        self.assertFalse(data["sections"]["programs"])
        self.assertEqual(data["programs"], [])

        # The club's own members get no exception.
        self.assertEqual(self.showcase(self.owner).data["data"]["programs"], [])

    def test_contact_numbers_only_while_on(self):
        self.org_patch(self.owner, "contact", {
            "whatsapp_enabled": True, "whatsapp_number": "9876543210",
            "email_enabled": True, "contact_email": "hello@bright.test",
        })
        contact = self.public_page().data["data"]["contact"]
        self.assertEqual(contact["whatsapp"], "+919876543210")
        self.assertEqual(contact["email"], "hello@bright.test")

        self.org_patch(self.owner, "contact", {"whatsapp_enabled": False})
        contact = self.public_page().data["data"]["contact"]
        self.assertIsNone(contact["whatsapp"])
        # Still stored, for when it is switched back on.
        self.assertEqual(
            PageSettings.objects.get(organization=self.org).whatsapp_number, "+919876543210"
        )

    def test_facts_and_facilities_only_with_something_in_them(self):
        self.seed_catalog()
        self.org_patch(self.owner, "facts", {"founded_year": 2010, "training_days": ["sat"]})
        self.org_put(self.owner, f"facilities/{self.branch.id}", {"items": ["gym"]})

        data = self.public_page().data["data"]
        self.assertEqual(data["facts"]["founded_year"], 2010)
        self.assertEqual(data["facts"]["links"], {"instagram": None, "youtube": None, "facebook": None})
        self.assertEqual(len(data["facilities"]), 1)
        self.assertEqual(data["facilities"][0]["items"], [{"key": "gym", "label": "Gym", "group": "training"}])

        # A branch with nothing is left out.
        self.org_put(self.owner, f"facilities/{self.branch.id}", {"items": []})
        self.assertEqual(self.public_page().data["data"]["facilities"], [])

    def test_a_private_org_is_a_404_to_the_anonymous_only(self):
        OrganizationProfile.objects.filter(organization=self.org).update(is_public_profile=False)
        self.assert_error(self.public_page(), "not_found", 404)
        self.assertEqual(self.showcase(self.p1).status_code, 200)

    def test_a_suspended_org_is_a_404_everywhere(self):
        Organization.objects.filter(id=self.org.id).update(is_suspended=True)
        self.assert_error(self.public_page(), "not_found", 404)
        self.assert_error(self.showcase(self.p1), "not_found", 404)

    def test_a_block_is_a_404(self):
        self.block(self.p1, target_org=self.org)
        self.assert_error(self.showcase(self.p1), "not_found", 404)
        self.assertEqual(self.showcase(self.p2).status_code, 200)

    def test_a_program_edit_and_a_contact_change_clear_the_cache(self):
        program_id = self.org_post(
            self.owner, "programs", program_body(self.football)
        ).data["data"]["id"]
        self.assertEqual(self.public_page().data["data"]["programs"][0]["name"], "U12 Football")

        self.org_patch(self.owner, f"programs/{program_id}", {"name": "U13 Football"})
        self.assertEqual(self.public_page().data["data"]["programs"][0]["name"], "U13 Football")

        self.org_patch(self.owner, "contact", {"call_enabled": True, "call_number": "9876543210"})
        self.assertEqual(self.public_page().data["data"]["contact"]["call"], "+919876543210")

    def test_the_privacy_setting_clears_the_cache(self):
        self.assertEqual(self.public_page().status_code, 200)
        self.client.force_authenticate(user=self.owner)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.patch(
                "/organizations/privacy/public-profile",
                {"is_public_profile": False}, format="json",
                HTTP_X_ACTOR_TYPE="organization", HTTP_X_ACTOR_ID=str(self.org.id),
            )
        self.assertEqual(response.status_code, 200, response.data)
        self.assert_error(self.public_page(), "not_found", 404)

    def test_member_coaches_follow_the_profile_rules(self):
        self.org_post(self.owner, "coaches", {
            "member_id": str(self.staff_member.id), "role": "head_coach",
        })
        coach = self.public_page().data["data"]["coaches"][0]
        self.assertEqual(coach["username"], "pagestaff")

        UserProfile.objects.filter(user=self.staff).update(is_public_profile=False)
        from django.core.cache import cache
        cache.clear()
        coach = self.public_page().data["data"]["coaches"][0]
        self.assertEqual(coach["name"], "Sam Staff")
        self.assertIsNone(coach["username"])
        # Signed in, the profile opens.
        self.assertEqual(self.showcase(self.p1).data["data"]["coaches"][0]["username"], "pagestaff")

    def test_never_an_enquiry_a_note_or_hidden_contact(self):
        self.org_patch(self.owner, "contact", {
            "call_enabled": False, "call_number": "9876543210",
        })
        data = self.public_page().data["data"]
        self.assertNotIn("9876543210", str(data))
        self.assertEqual(
            find_keys(data, {"notes", "enquiries", "phone", "guardian_name"}), set()
        )
