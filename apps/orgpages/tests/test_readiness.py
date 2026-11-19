"""Readiness on the hub: weights, the switched-off rescale, and what's next."""

from apps.organization.models import OrganizationProfile
from apps.orgpages.models import Coach, FacilityPhoto
from apps.orgpages.tests.base import PageTestBase


class ReadinessTests(PageTestBase):

    def readiness(self):
        return self.org_get(self.owner, "admin").data["data"]["readiness"]

    def test_the_weights_and_next(self):
        # The fixture academy: a branch (location 10) and its form on by
        # default (contact 10). A logo but no cover; no description.
        readiness = self.readiness()
        self.assertEqual(readiness["percent"], 20)
        self.assertEqual(readiness["next"], {"key": "programs", "label": "Add your programs"})
        self.assertEqual(
            [item["key"] for item in readiness["items"]],
            ["programs", "facilities", "coaches", "about", "facts", "photos", "location", "contact"],
        )

        self.make_program()
        self.assertEqual(self.readiness()["percent"], 40)
        self.assertEqual(self.readiness()["next"]["key"], "facilities")

        key = self.page_key()
        FacilityPhoto.objects.create(location=self.branch, photo_url=f"https://x/{key}", photo_public_id=key)
        Coach.objects.create(organization=self.org, name="Ravi", role="head_coach")
        self.assertEqual(self.readiness()["percent"], 70)
        self.assertEqual(self.readiness()["next"]["key"], "about")

        OrganizationProfile.objects.filter(organization=self.org).update(
            description="We train young footballers in Kochi every evening of the week.",
            cover_image="https://media.example.com/cover.webp",
        )
        self.org_patch(self.owner, "facts", {
            "founded_year": 2010, "age_min": 6, "coaching_languages": ["English"],
        })
        readiness = self.readiness()
        self.assertEqual(readiness["percent"], 100)
        self.assertIsNone(readiness["next"])

    def test_facts_need_three_and_the_age_range_is_one(self):
        self.org_patch(self.owner, "facts", {"age_min": 6, "age_max": 12, "founded_year": 2010})
        facts = next(i for i in self.readiness()["items"] if i["key"] == "facts")
        self.assertFalse(facts["done"])

    def test_a_hidden_coach_does_not_count(self):
        Coach.objects.create(organization=self.org, name="Ravi", role="head_coach", is_visible=False)
        coaches = next(i for i in self.readiness()["items"] if i["key"] == "coaches")
        self.assertFalse(coaches["done"])

    def test_a_switched_off_section_rescales(self):
        self.org_patch(self.owner, "sections", {"programs": False})
        readiness = self.readiness()
        # 20 of the remaining 80.
        self.assertEqual(readiness["percent"], 25)
        self.assertNotIn("programs", [item["key"] for item in readiness["items"]])
        self.assertEqual(readiness["next"]["key"], "facilities")

        self.org_patch(self.owner, "sections", {
            "facilities": False, "coaches": False, "about": False, "locations": False,
        })
        readiness = self.readiness()
        # facts, photos, contact are left: 10 of 30.
        self.assertEqual(readiness["percent"], 33)
        self.assertEqual(readiness["next"]["key"], "facts")

    def test_the_hub_counts(self):
        self.make_program()
        Coach.objects.create(organization=self.org, name="Ravi", role="head_coach")
        sections = self.org_get(self.owner, "admin").data["data"]["sections"]
        self.assertEqual(sections["programs"], {"enabled": True, "switchable": True, "count": 1})
        self.assertEqual(sections["coaches"]["count"], 1)
        self.assertEqual(sections["locations"]["count"], 1)
        self.assertEqual(sections["facilities"], {"enabled": True, "switchable": True, "items": 0, "photos": 0})
        self.assertEqual(sections["posts"]["switchable"], False)
        self.assertFalse(sections["about"]["filled"])
