"""Facilities per branch, the catalog and branch photos."""

from apps.organization.models import OrganizationLocation
from apps.orgpages.models import FacilityItem, FacilityPhoto
from apps.orgpages.tests.base import PageTestBase


class CatalogSeedTests(PageTestBase):

    def test_the_seed_is_idempotent_and_resets_what_it_seeds(self):
        self.seed_catalog()
        self.assertEqual(FacilityItem.objects.count(), 19)

        FacilityItem.objects.filter(key="gym").update(label="Weights room", is_active=False)
        self.seed_catalog()

        self.assertEqual(FacilityItem.objects.count(), 19)
        gym = FacilityItem.objects.get(key="gym")
        self.assertEqual(gym.label, "Gym")
        self.assertTrue(gym.is_active)
        self.assertEqual(
            FacilityItem.objects.get(key="pickup_drop").group, FacilityItem.Group.CARE_TRAVEL
        )


class FacilityTests(PageTestBase):

    def setUp(self):
        super().setUp()
        self.seed_catalog()

    def put(self, body, location=None):
        return self.org_put(self.owner, f"facilities/{(location or self.branch).id}", body)

    def test_set_and_read(self):
        response = self.put({
            "items": ["gym", "floodlights", "gym"],
            "custom_items": ["Swimming pool", "swimming POOL"],
        })
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["items"], ["floodlights", "gym"])
        self.assertEqual(response.data["data"]["custom_items"], ["Swimming pool"])

        data = self.org_get(self.owner, "facilities").data["data"]
        self.assertEqual(
            [group["group"] for group in data["catalog"]],
            ["playing_area", "training", "comfort", "care_travel"],
        )
        self.assertEqual(data["locations"][0]["items"], ["floodlights", "gym"])

        # A PUT replaces both lists.
        self.put({"items": [], "custom_items": []})
        data = self.org_get(self.owner, "facilities").data["data"]
        self.assertEqual(data["locations"][0]["items"], [])

    def test_an_unknown_or_inactive_key_is_refused(self):
        self.assert_error(self.put({"items": ["helipad"]}), "invalid", field="items")
        FacilityItem.objects.filter(key="gym").update(is_active=False)
        self.assert_error(self.put({"items": ["gym"]}), "invalid", field="items")

    def test_custom_items_at_most_ten(self):
        self.assert_error(
            self.put({"items": [], "custom_items": [f"Thing {i}" for i in range(11)]}),
            "invalid", field="custom_items",
        )
        self.assert_error(
            self.put({"items": [], "custom_items": ["x"]}), "invalid", field="custom_items"
        )

    def test_another_orgs_branch_is_not_found(self):
        other = self.make_org("Other", "otheracademy")
        far = OrganizationLocation.objects.create(
            organization=other, name="Far", city="Delhi", country_code="IN"
        )
        self.assert_error(self.put({"items": ["gym"]}, location=far), "not_found", 404)

    def test_photos_at_most_twelve(self):
        response = self.org_post(
            self.owner, f"facilities/{self.branch.id}/photos", self.photo_body()
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(len(response.data["data"]["photos"]), 1)

        for i in range(11):
            key = self.page_key()
            FacilityPhoto.objects.create(
                location=self.branch, photo_url=f"https://x/{key}", photo_public_id=key, order=i + 1
            )
        self.assert_error(
            self.org_post(self.owner, f"facilities/{self.branch.id}/photos", self.photo_body()),
            "photo_limit",
        )

    def test_a_photo_must_come_from_the_page_folder(self):
        squads_key = f"organizations/{self.org.id}/squads/x.webp"
        for body in (
            {"photo_url": f"https://media.example.com/{squads_key}", "photo_public_id": squads_key},
            {"photo_url": "https://evil.example.com/a.webp", "photo_public_id": self.page_key()},
            {},
        ):
            self.assert_error(
                self.org_post(self.owner, f"facilities/{self.branch.id}/photos", body),
                "invalid_photo",
            )

    def test_a_photo_delete_enqueues_the_storage_delete(self):
        key = self.page_key()
        self.org_post(self.owner, f"facilities/{self.branch.id}/photos", self.photo_body(key))
        photo = FacilityPhoto.objects.get(photo_public_id=key)
        self.storage.assert_not_called()

        response = self.org_post(self.owner, f"facilities/photos/{photo.id}/delete")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(FacilityPhoto.objects.filter(id=photo.id).exists())
        self.assertEqual(self.storage.call_args.args[1], ([key],))

    def test_photo_reorder_must_be_exact(self):
        keys = [self.page_key() for _ in range(3)]
        for key in keys:
            self.org_post(self.owner, f"facilities/{self.branch.id}/photos", self.photo_body(key))
        ids = [str(p.id) for p in FacilityPhoto.objects.filter(location=self.branch).order_by("order")]

        self.assert_error(
            self.org_post(self.owner, f"facilities/{self.branch.id}/photos/reorder", {"ids": ids[:2]}),
            "invalid", field="ids",
        )
        response = self.org_post(
            self.owner, f"facilities/{self.branch.id}/photos/reorder", {"ids": ids[::-1]}
        )
        self.assertEqual([p["id"] for p in response.data["data"]["photos"]], ids[::-1])
