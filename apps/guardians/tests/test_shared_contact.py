"""
The same-address case: a child names the email they signed up with.

There is no separate route for it any more. The link goes out exactly as it
would to any other address — the honest alternative, a hand-the-phone approval
on the child's own device, let anybody holding the phone tick "I'm the parent"
— and what makes the case different is the LABEL on the answer: an approval
that came back through an inbox the child can open is recorded as
``shared_contact``, the weakest of the three methods, so it stays countable and
can be re-verified when the DPDP rules say what a verified parent looks like.

The tests are written to keep it visibly weak. If these approvals ever start
recording as something stronger, the DPDP question "how do you know it was the
parent" gets a worse answer than the truth.
"""

from apps.guardians.constants import (
    GuardianConsentLevel,
    GuardianConsentMethod,
    token_hash,
)
from apps.guardians.models import Guardian, GuardianConsentEvent
from apps.guardians.services.consent_service import request_consent
from apps.guardians.tests.base import (
    FEED_URL,
    GUARDIAN_DETAILS_URL,
    PARENT_NAME,
    GuardianTestCase,
    make_minor,
)
from apps.accounts.models import User

CHILD_EMAIL = "sharedchild@example.com"
CHILD_PHONE = "+919876500011"

SHARED_APPROVE_URL = "/guardian/shared/approve"


class SameAddressRequestTests(GuardianTestCase):

    def setUp(self):
        super().setUp()
        self.child = self.authenticate(
            make_minor(email=CHILD_EMAIL, username="sharedchild")
        )

    def test_the_link_is_sent_like_any_other(self):
        response, token = self.ask_for_consent(parent_email=CHILD_EMAIL)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["mode"], "link_sent")
        self.assertTrue(response.data["data"]["same_as_login_contact"])
        self.assertIsNotNone(token)

    def test_the_email_goes_to_that_address(self):
        with self.sending_consent_email() as sender:
            self.client.post(
                GUARDIAN_DETAILS_URL,
                {"parent_name": PARENT_NAME, "parent_email": CHILD_EMAIL},
                format="json",
            )

        sender.assert_called_once()
        self.assertEqual(sender.call_args.kwargs["email"], CHILD_EMAIL)

    def test_a_real_token_is_minted(self):
        # A link with nothing behind it would be a link that resolves to a 404
        # in the parent's hands. The hash and the deadline are stored exactly
        # as they are for any other address.
        _, token = self.ask_for_consent(parent_email=CHILD_EMAIL)

        event = GuardianConsentEvent.objects.get(child=self.child)
        self.assertEqual(event.event_type, "requested")
        self.assertEqual(event.token_hash, token_hash(token))
        self.assertIsNotNone(event.token_expires_at)

    def test_case_does_not_hide_a_shared_contact(self):
        # "SharedChild@Example.COM" is the same inbox, and the label has to
        # say so — otherwise the approval would be recorded as a stronger
        # thing than it is.
        response, token = self.ask_for_consent(
            parent_email=CHILD_EMAIL.upper()
        )

        self.assertEqual(response.data["data"]["mode"], "link_sent")
        self.assertTrue(response.data["data"]["same_as_login_contact"])
        self.assertIsNotNone(token)

    def test_a_different_address_is_not_shared(self):
        response, token = self.ask_for_consent(parent_email="mum@example.com")

        self.assertEqual(response.data["data"]["mode"], "link_sent")
        self.assertFalse(response.data["data"]["same_as_login_contact"])
        self.assertIsNotNone(token)

    def test_the_guardian_is_not_linked_to_the_childs_own_account(self):
        # The address matches an existing account — the child's. Linking it
        # would make the child their own guardian on paper, and the link is
        # read as "a known adult stands behind this contact".
        self.ask_for_consent(parent_email=CHILD_EMAIL)

        guardian = Guardian.objects.get(email=CHILD_EMAIL)
        self.assertIsNone(guardian.linked_user_id)

    def test_a_phone_login_is_detected_too(self):
        """
        The service handles a phone contact the same way — this goes through
        it directly because the HTTP endpoint refuses phone numbers today
        (there is no SMS sender), so the rule would otherwise be untested until
        one lands.
        """
        child = make_minor(
            email="phonekid@example.com", username="phonekid", phone=CHILD_PHONE
        )

        result = request_consent(
            child=child, parent_name=PARENT_NAME, parent_contact=CHILD_PHONE
        )

        self.assertTrue(result["same_as_login_contact"])
        self.assertEqual(result["delivery"], "none")


class SameAddressApprovalTests(GuardianTestCase):

    def setUp(self):
        super().setUp()
        self.child = self.authenticate(
            make_minor(email=CHILD_EMAIL, username="sharedchild")
        )
        _, self.token = self.ask_for_consent(parent_email=CHILD_EMAIL)

    def test_the_link_unlocks_the_child(self):
        res = self.approve_by_link(self.token)

        self.assertEqual(res.status_code, 200, res.data)
        self.assert_status(self.child, User.GuardianConsentStatus.APPROVED)
        self.assertEqual(self.client.get(FEED_URL).status_code, 200)

    def test_the_event_records_the_weak_method(self):
        self.approve_by_link(self.token)

        event = GuardianConsentEvent.objects.get(
            child=self.child, event_type="approved"
        )
        self.assertEqual(event.method, GuardianConsentMethod.SHARED_CONTACT)
        self.assertEqual(event.level, GuardianConsentLevel.ACKNOWLEDGED)
        self.assertEqual(event.notice_version, "2026-10-01")

    def test_the_parents_own_name_is_recorded_separately(self):
        # Guardian.name is what the CHILD said; parent_name_given is what the
        # person answering typed. The two disagreeing is a signal, so they are
        # never collapsed into one column.
        self.approve_by_link(self.token, parent_name="Priya S Nair")

        event = GuardianConsentEvent.objects.get(
            child=self.child, event_type="approved"
        )
        self.assertEqual(event.parent_name_given, "Priya S Nair")
        self.assertEqual(event.guardian.name, PARENT_NAME)

    def test_resend_works_for_a_same_address_request(self):
        response, fresh_token = self.resend_consent()

        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["data"]["same_as_login_contact"])
        self.assertIsNotNone(fresh_token)
        self.assertNotEqual(fresh_token, self.token)

        # The fresh link works, and it is labelled the same honest way.
        self.assertEqual(self.approve_by_link(fresh_token).status_code, 200)
        event = GuardianConsentEvent.objects.get(
            child=self.child, event_type="approved"
        )
        self.assertEqual(event.method, GuardianConsentMethod.SHARED_CONTACT)

    def test_one_guardian_row_for_the_shared_contact(self):
        self.approve_by_link(self.token)

        # The guardian created for a shared contact holds the child's own
        # address — which is exactly why the method is recorded as the weak one.
        self.assertEqual(Guardian.objects.filter(email=CHILD_EMAIL).count(), 1)
        self.assertEqual(
            GuardianConsentEvent.objects
            .filter(child=self.child)
            .first()
            .guardian.email,
            CHILD_EMAIL,
        )


class TheOnDeviceRouteIsGoneTests(GuardianTestCase):
    """
    The bypass the old route opened: a child could name a parent's real
    address, let the link go out, and then approve themselves from their own
    device — or simply be anybody holding the phone. There is no endpoint for
    it now, on any account state.
    """

    def setUp(self):
        super().setUp()
        self.child = self.authenticate(
            make_minor(email=CHILD_EMAIL, username="sharedchild")
        )

    def _post(self):
        return self.client.post(
            SHARED_APPROVE_URL,
            {"parent_name": PARENT_NAME, "confirm_18_plus": True},
            format="json",
        )

    def test_it_is_a_404_after_a_same_address_request(self):
        self.ask_for_consent(parent_email=CHILD_EMAIL)

        self.assertEqual(self._post().status_code, 404)
        self.assert_status(self.child, User.GuardianConsentStatus.PENDING)

    def test_it_is_a_404_after_a_link_went_to_a_parent(self):
        self.ask_for_consent(parent_email="mum@example.com")

        self.assertEqual(self._post().status_code, 404)
        self.assert_status(self.child, User.GuardianConsentStatus.PENDING)

    def test_it_writes_nothing(self):
        self.ask_for_consent(parent_email=CHILD_EMAIL)
        self._post()

        self.assertFalse(
            GuardianConsentEvent.objects
            .filter(child=self.child, event_type="approved")
            .exists()
        )
