"""Contact options, key facts, section switches — and the one phone helper."""

from django.test import SimpleTestCase
from django.utils import timezone

from apps.organization.models import Organization
from apps.orgpages.models import PageSettings
from apps.orgpages.phones import InvalidPhone, mask_phone, normalize_phone
from apps.orgpages.services.guards import page_settings_for
from apps.orgpages.tests.base import PageTestBase


class PhoneNormalizationTests(SimpleTestCase):

    def test_the_table(self):
        table = {
            "9876543210": "+919876543210",
            "98765 43210": "+919876543210",
            "98765-43210": "+919876543210",
            "(98765) 43.210": "+919876543210",
            "09876543210": "+919876543210",
            "919876543210": "+919876543210",
            "+91 98765 43210": "+919876543210",
            "+44 20 7946 0958": "+442079460958",
            "+12345678": "+12345678",
            "6123456789": "+916123456789",
        }
        for raw, expected in table.items():
            self.assertEqual(normalize_phone(raw), expected, raw)

    def test_anything_else_is_refused(self):
        for raw in (
            "", "   ", "12345", "5876543210", "1876543210", "+1234567",
            "+1234567890123456", "98765432101", "abcdefghij", "+91 98765 4321x",
            None, 9876543210,
        ):
            with self.assertRaises(InvalidPhone, msg=repr(raw)):
                normalize_phone(raw)

    def test_the_mask(self):
        self.assertEqual(mask_phone("+919876543210"), "+91 98•••• 3210")
        self.assertNotIn("98765", mask_phone("+919876543210"))


class FormDefaultTests(PageTestBase):

    def test_on_for_an_academy_off_for_a_club_and_a_team(self):
        self.assertTrue(page_settings_for(self.org).enquiry_form_enabled)
        club = self.make_org("A Club", "aclub", Organization.Type.CLUB)
        team = self.make_org("A Team", "ateam", Organization.Type.TEAM)
        self.assertFalse(page_settings_for(club).enquiry_form_enabled)
        self.assertFalse(page_settings_for(team).enquiry_form_enabled)

    def test_the_row_is_created_lazily_once(self):
        self.assertFalse(PageSettings.objects.filter(organization=self.org).exists())
        self.org_get(self.owner, "admin")
        self.org_get(self.owner, "admin")
        self.assertEqual(PageSettings.objects.filter(organization=self.org).count(), 1)


class ContactTests(PageTestBase):

    def test_numbers_are_normalized(self):
        response = self.org_patch(self.owner, "contact", {
            "whatsapp_enabled": True, "whatsapp_number": "098765 43210",
            "call_enabled": True, "call_number": "+44 20 7946 0958",
        })
        self.assertEqual(response.status_code, 200, response.data)
        contact = response.data["data"]["contact"]
        self.assertEqual(contact["whatsapp_number"], "+919876543210")
        self.assertEqual(contact["call_number"], "+442079460958")

    def test_a_switch_on_without_its_number_is_refused(self):
        self.assert_error(
            self.org_patch(self.owner, "contact", {"whatsapp_enabled": True}),
            "invalid", field="whatsapp_number",
        )
        self.assert_error(
            self.org_patch(self.owner, "contact", {"call_enabled": True}),
            "invalid", field="call_number",
        )
        self.assert_error(
            self.org_patch(self.owner, "contact", {"email_enabled": True}),
            "invalid", field="contact_email",
        )
        self.assert_error(
            self.org_patch(self.owner, "contact", {
                "email_enabled": True, "contact_email": "not-an-email",
            }),
            "invalid", field="contact_email",
        )
        self.assert_error(
            self.org_patch(self.owner, "contact", {"whatsapp_number": "12345"}),
            "invalid", field="whatsapp_number",
        )

    def test_clearing_a_number_while_its_switch_is_on_is_refused(self):
        self.org_patch(self.owner, "contact", {
            "call_enabled": True, "call_number": "9876543210",
        })
        self.assert_error(
            self.org_patch(self.owner, "contact", {"call_number": ""}),
            "invalid", field="call_number",
        )


class FactsTests(PageTestBase):

    def patch_facts(self, body):
        return self.org_patch(self.owner, "facts", body)

    def test_ranges(self):
        year = timezone.now().year
        for body, field in (
            ({"founded_year": 1849}, "founded_year"),
            ({"founded_year": year + 1}, "founded_year"),
            ({"age_min": 2}, "age_min"),
            ({"age_max": 81}, "age_max"),
            ({"age_min": 12, "age_max": 8}, "age_max"),
            ({"age_min": True}, "age_min"),
            ({"student_range": "lots"}, "student_range"),
        ):
            self.assert_error(self.patch_facts(body), "invalid", field=field)

        response = self.patch_facts({"founded_year": 1850, "age_min": 3, "age_max": 80})
        self.assertEqual(response.status_code, 200, response.data)

    def test_languages_are_trimmed_and_deduplicated(self):
        response = self.patch_facts({
            "coaching_languages": [" English ", "english", "Malayalam", "HINDI", "hindi"],
        })
        self.assertEqual(
            response.data["data"]["facts"]["coaching_languages"],
            ["English", "Malayalam", "HINDI"],
        )
        self.assert_error(
            self.patch_facts({"coaching_languages": [f"L{i}" for i in range(7)]}),
            "invalid", field="coaching_languages",
        )
        self.assert_error(
            self.patch_facts({"coaching_languages": ["x" * 31]}),
            "invalid", field="coaching_languages",
        )

    def test_days_are_stored_in_week_order(self):
        response = self.patch_facts({"training_days": ["sun", "mon", "WED", "mon"]})
        self.assertEqual(response.data["data"]["facts"]["training_days"], ["mon", "wed", "sun"])
        self.assert_error(self.patch_facts({"training_days": ["funday"]}), "invalid", field="training_days")

    def test_social_links_must_be_https_on_their_own_site(self):
        good = {
            "instagram_url": "https://www.instagram.com/brightacademy",
            "youtube_url": "https://youtu.be/abc123",
            "facebook_url": "https://fb.com/brightacademy",
        }
        response = self.patch_facts(good)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["facts"]["youtube_url"], good["youtube_url"])

        self.assertEqual(
            self.patch_facts({"youtube_url": "https://m.youtube.com/@bright"}).status_code, 200
        )
        for field, url in (
            ("instagram_url", "http://instagram.com/x"),
            ("instagram_url", "https://instagram.com.evil.com/x"),
            ("instagram_url", "https://evilinstagram.com/x"),
            ("youtube_url", "https://facebook.com/x"),
            ("facebook_url", "https://user:pw@facebook.com/x"),
            ("facebook_url", "javascript:alert(1)"),
        ):
            self.assert_error(self.patch_facts({field: url}), "invalid", field=field)

    def test_partial(self):
        self.patch_facts({"founded_year": 2001})
        response = self.patch_facts({"age_min": 5})
        facts = response.data["data"]["facts"]
        self.assertEqual(facts["founded_year"], 2001)
        self.assertEqual(facts["age_min"], 5)


class SectionTests(PageTestBase):

    def test_switches(self):
        response = self.org_patch(self.owner, "sections", {"programs": False})
        self.assertEqual(response.status_code, 200, response.data)
        sections = response.data["data"]["sections"]
        self.assertFalse(sections["programs"])
        self.assertTrue(sections["coaches"])

    def test_an_unknown_key_or_posts_is_refused(self):
        self.assert_error(
            self.org_patch(self.owner, "sections", {"gallery": False}), "invalid", field="gallery"
        )
        self.assert_error(
            self.org_patch(self.owner, "sections", {"posts": False}), "invalid", field="posts"
        )
        self.assert_error(
            self.org_patch(self.owner, "sections", {"about": "no"}), "invalid", field="about"
        )
