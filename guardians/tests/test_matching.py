"""
Which parent is which — the find-or-create rule, and the sibling hint built on
top of it.

One family, one row. That is the whole claim of this file, and it is worth
testing hard in both directions: two children of the same parent must resolve to
ONE Guardian, and two different parents who happen to share a name must never
collapse into one. Get the first wrong and a parent who approved in September
gets asked from scratch in October; get the second wrong and one family's
consent history is attached to another family's child.

The identity is the CONTACT, never the name. A contact is the only part of a
guardian we can actually reach, and names are not unique — there is more than
one Priya Nair.
"""

from guardians.models import Guardian, GuardianConsentEvent
from guardians.selectors.consent_selectors import (
    guardian_other_approved_children,
)
from guardians.services.consent_service import request_consent
from guardians.tests.base import (
    PARENT_EMAIL,
    PARENT_NAME,
    GuardianTestCase,
    make_adult,
    make_minor,
)
from accounts.models import User


class OneParentManyChildrenTests(GuardianTestCase):

    def setUp(self):
        super().setUp()
        self.first = make_minor(email="kid1@example.com", username="matchkid1")
        self.second = make_minor(email="kid2@example.com", username="matchkid2")

    def _ask(self, child, parent_email=PARENT_EMAIL, parent_name=PARENT_NAME):
        self.authenticate(child)
        return self.ask_for_consent(
            parent_email=parent_email, parent_name=parent_name
        )

    def test_two_children_map_to_one_guardian(self):
        self._ask(self.first)
        self._ask(self.second)

        self.assertEqual(Guardian.objects.filter(email=PARENT_EMAIL).count(), 1)

        guardians = set(
            GuardianConsentEvent.objects.values_list("guardian_id", flat=True)
        )
        self.assertEqual(len(guardians), 1)

    def test_the_match_is_case_insensitive(self):
        self._ask(self.first, parent_email="Priya@Example.COM")
        self._ask(self.second, parent_email="priya@example.com")

        self.assertEqual(Guardian.objects.count(), 1)
        # Stored lowercased, so the index lookup that finds it can be exact.
        self.assertEqual(Guardian.objects.get().email, "priya@example.com")

    def test_a_second_child_does_not_rewrite_the_parents_name(self):
        # The younger sibling calls her "Amma". Overwriting would rewrite what
        # the first child's consent record says about who was asked.
        self._ask(self.first, parent_name="Priya Nair")
        self._ask(self.second, parent_name="Amma")

        self.assertEqual(Guardian.objects.get().name, "Priya Nair")

    def test_a_different_address_is_a_different_parent(self):
        self._ask(self.first, parent_email="mum@example.com")
        self._ask(self.second, parent_email="dad@example.com")

        self.assertEqual(Guardian.objects.count(), 2)


class MatchingIsNeverByNameTests(GuardianTestCase):

    def test_the_same_name_at_two_addresses_is_two_parents(self):
        # There is more than one Priya Nair. Merging on the name would attach
        # one family's consent history to another family's child.
        first = make_minor(email="kid1@example.com", username="namekid1")
        second = make_minor(email="kid2@example.com", username="namekid2")

        self.authenticate(first)
        self.ask_for_consent(parent_email="priya1@example.com", parent_name=PARENT_NAME)
        self.authenticate(second)
        self.ask_for_consent(parent_email="priya2@example.com", parent_name=PARENT_NAME)

        self.assertEqual(
            Guardian.objects.filter(name=PARENT_NAME).count(), 2
        )

    def test_a_different_name_at_the_same_address_is_one_parent(self):
        first = make_minor(email="kid1@example.com", username="namekid1")
        second = make_minor(email="kid2@example.com", username="namekid2")

        self.authenticate(first)
        self.ask_for_consent(parent_email=PARENT_EMAIL, parent_name="Priya Nair")
        self.authenticate(second)
        self.ask_for_consent(parent_email=PARENT_EMAIL, parent_name="P. Nair")

        self.assertEqual(Guardian.objects.count(), 1)


class LinkedUserTests(GuardianTestCase):
    """
    A parent who already has a Goatza account of their own — usually a coach.
    The link is a convenience, never an ownership claim, and it is filled
    opportunistically so the first feature to want it needs no backfill.
    """

    def test_a_matching_account_is_linked(self):
        parent_user = make_adult(
            email="coachdad@example.com", username="coachdad"
        )
        child = self.authenticate(make_minor())

        self.ask_for_consent(parent_email="coachdad@example.com")

        guardian = Guardian.objects.get(email="coachdad@example.com")
        self.assertEqual(guardian.linked_user_id, parent_user.id)

    def test_an_unknown_address_links_nothing(self):
        self.authenticate(make_minor())

        self.ask_for_consent(parent_email="stranger@example.com")

        guardian = Guardian.objects.get(email="stranger@example.com")
        self.assertIsNone(guardian.linked_user_id)

    def test_an_adult_linked_account_upgrades_the_method(self):
        # The one thing the link changes today: an approval from a known adult
        # account is a stronger record than one from an address we know nothing
        # about, and the two must stay distinguishable in the table.
        make_adult(email="coachdad@example.com", username="coachdad")
        child = self.authenticate(make_minor())

        _, token = self.ask_for_consent(parent_email="coachdad@example.com")
        self.approve_by_link(token)

        event = GuardianConsentEvent.objects.get(
            child=child, event_type="approved"
        )
        self.assertEqual(event.method, "goatza_account")

    def test_a_linked_account_with_no_birthdate_stays_separate_contact(self):
        # Every uncertainty reads as "we cannot claim this was a known adult".
        parent_user = make_adult(email="nobd@example.com", username="nobdparent")
        parent_user.profile.birthdate = None
        parent_user.profile.save(update_fields=["birthdate"])

        child = self.authenticate(make_minor())
        _, token = self.ask_for_consent(parent_email="nobd@example.com")
        self.approve_by_link(token)

        event = GuardianConsentEvent.objects.get(
            child=child, event_type="approved"
        )
        self.assertEqual(event.method, "separate_contact")


class SiblingHintTests(GuardianTestCase):

    def setUp(self):
        super().setUp()
        self.first = make_minor(email="kid1@example.com", username="sibkid1")
        self.second = make_minor(email="kid2@example.com", username="sibkid2")

        self.authenticate(self.first)
        _, self.first_token = self.ask_for_consent()
        self.approve_by_link(self.first_token)

        self.authenticate(self.second)
        _, self.second_token = self.ask_for_consent()

        self.guardian = Guardian.objects.get(email=PARENT_EMAIL)

    def test_the_hint_names_the_approved_sibling(self):
        siblings = list(
            guardian_other_approved_children(
                self.guardian, exclude_child=self.second
            )
        )

        self.assertEqual([user.id for user in siblings], [self.first.id])

    def test_the_child_being_asked_about_is_excluded(self):
        self.approve_by_link(self.second_token)

        siblings = list(
            guardian_other_approved_children(
                self.guardian, exclude_child=self.second
            )
        )

        self.assertNotIn(self.second.id, [user.id for user in siblings])

    def test_a_pending_sibling_is_not_a_hint(self):
        # The hint says "this parent has already approved somebody", so a child
        # who was only ASKED about does not belong in it.
        siblings = list(
            guardian_other_approved_children(
                self.guardian, exclude_child=self.first
            )
        )

        self.assertEqual(siblings, [])

    def test_a_withdrawn_sibling_drops_out(self):
        # Reading only the events would keep a child here whose consent was
        # revoked last week — precisely the family this must not get wrong.
        from unittest.mock import patch

        with patch(
            "guardians.services.consent_service"
            ".send_guardian_consent_withdrawn_email"
        ):
            self.parent_post(f"/guardian/consent/{self.first_token}/withdraw")

        siblings = list(
            guardian_other_approved_children(
                self.guardian, exclude_child=self.second
            )
        )

        self.assertEqual(siblings, [])

    def test_the_parents_page_shows_the_hint(self):
        # The selector is only useful if the page actually carries it.
        res = self.parent_get(f"/guardian/consent/{self.second_token}")

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["data"]["siblings"], ["sibkid1"])
