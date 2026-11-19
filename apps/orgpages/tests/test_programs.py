"""Programs & fees."""

from apps.organization.models import OrganizationLocation, OrganizationSport
from apps.orgpages.models import Program
from apps.orgpages.tests.base import PageTestBase, program_body


class ProgramTests(PageTestBase):

    def create(self, **overrides):
        return self.org_post(self.owner, "programs", program_body(self.football, **overrides))

    def test_create_and_list(self):
        response = self.create(location_id=str(self.branch.id), free_trial=True, spots="few")
        self.assertEqual(response.status_code, 201, response.data)
        data = response.data["data"]
        self.assertEqual(data["days"], ["mon", "sat"])
        self.assertEqual(data["start_time"], "16:30")
        self.assertEqual(data["end_time"], "18:00")
        self.assertEqual(data["fee"], {"mode": "amount", "amount": 1500, "currency": "INR", "per": "month"})
        self.assertEqual(data["location"]["id"], str(self.branch.id))
        self.assertEqual(data["level_label"], "Beginner")
        self.assertEqual(data["spots_label"], "Few spots left")

        listed = self.org_get(self.owner, "programs").data["data"]["programs"]
        self.assertEqual([p["id"] for p in listed], [data["id"]])

    def test_staff_can_manage_programs(self):
        self.assertEqual(
            self.org_post(self.staff, "programs", program_body(self.football)).status_code, 201
        )

    def test_fee_amount_vs_contact(self):
        self.assert_error(self.create(fee_per=None), "invalid", field="fee_per")
        self.assert_error(self.create(fee_amount=None), "invalid", field="fee_amount")
        self.assert_error(self.create(fee_amount=0), "invalid", field="fee_amount")
        self.assert_error(self.create(fee_amount=1_000_001), "invalid", field="fee_amount")
        self.assert_error(self.create(fee_per="fortnight"), "invalid", field="fee_per")

        response = self.create(fee_mode="contact")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["data"]["fee"]["amount"], None)
        self.assertEqual(response.data["data"]["fee"]["per"], None)

        # Switching an existing program to an amount needs both halves.
        program_id = response.data["data"]["id"]
        self.assert_error(
            self.org_patch(self.owner, f"programs/{program_id}", {"fee_mode": "amount"}),
            "invalid", field="fee_amount",
        )
        response = self.org_patch(self.owner, f"programs/{program_id}", {
            "fee_mode": "amount", "fee_amount": 900, "fee_per": "session",
        })
        self.assertEqual(response.data["data"]["fee"]["per"], "session")

    def test_ends_after_it_starts_and_ages_in_order(self):
        self.assert_error(self.create(end_time="16:30"), "invalid", field="end_time")
        self.assert_error(self.create(end_time="15:00"), "invalid", field="end_time")
        self.assert_error(self.create(start_time="25:00"), "invalid", field="start_time")
        self.assert_error(self.create(age_min=12, age_max=8), "invalid", field="age_max")
        self.assert_error(self.create(age_min=2), "invalid", field="age_min")
        self.assert_error(self.create(days=[]), "invalid", field="days")

    def test_the_orgs_own_sport_and_location(self):
        self.assert_error(
            self.create(sport_id=str(self.cricket.id)), "invalid", field="sport_id"
        )
        other = self.make_org("Other", "otheracademy")
        far = OrganizationLocation.objects.create(
            organization=other, name="Far", city="Delhi", country_code="IN"
        )
        self.assert_error(self.create(location_id=str(far.id)), "invalid", field="location_id")

    def test_no_sport(self):
        OrganizationSport.objects.filter(organization=self.org).delete()
        self.assert_error(self.create(), "no_sport", field="sport_id")
        self.assertEqual(
            self.org_post(self.owner, "programs", {}).data["data"]["code"], "no_sport"
        )

    def test_max_thirty(self):
        for i in range(30):
            self.make_program(f"P{i}")
        self.assert_error(self.create(), "program_limit")

    def test_partial_update_and_delete(self):
        program = self.make_program("Weekend")
        response = self.org_patch(self.owner, f"programs/{program.id}", {"name": "Weekend Squad"})
        self.assertEqual(response.data["data"]["name"], "Weekend Squad")
        self.assertEqual(response.data["data"]["age_min"], 8)

        self.assertEqual(
            self.org_post(self.owner, f"programs/{program.id}/delete").status_code, 200
        )
        self.assertFalse(Program.objects.filter(id=program.id).exists())
        self.assert_error(
            self.org_post(self.owner, f"programs/{program.id}/delete"), "not_found", 404
        )

    def test_another_orgs_program_reads_like_a_missing_one(self):
        other = self.make_org("Other", "otheracademy")
        program = self.make_program("Theirs", org=other)
        self.assert_error(
            self.org_patch(self.owner, f"programs/{program.id}", {"name": "Mine now"}),
            "not_found", 404,
        )

    def test_reorder_must_be_exact(self):
        a, b, c = (self.make_program(name) for name in ("A", "B", "C"))
        self.assert_error(
            self.org_post(self.owner, "programs/reorder", {"ids": [str(a.id), str(b.id)]}),
            "invalid", field="ids",
        )
        self.assert_error(
            self.org_post(self.owner, "programs/reorder", {
                "ids": [str(a.id), str(b.id), str(c.id), str(c.id)],
            }),
            "invalid", field="ids",
        )
        response = self.org_post(self.owner, "programs/reorder", {
            "ids": [str(c.id), str(a.id), str(b.id)],
        })
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            [p["name"] for p in response.data["data"]["programs"]], ["C", "A", "B"]
        )
