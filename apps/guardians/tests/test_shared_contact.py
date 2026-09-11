"""
The hand-the-phone route: when the only contact a child has for their parent is
the one they log in with themselves.

This is the weak path and the tests are written to keep it visibly weak. An
approval collected on the child's own device, through the child's own inbox,
proves that somebody with access to that inbox agreed and NOTHING more — so the
row it writes says ``shared_contact``, and every test here checks that label as
carefully as it checks the unlock. If these approvals ever start recording as
something stronger, the DPDP question "how do you know it was the parent" gets a
worse answer than the truth.

The other half of the design is that no email goes out. Mailing a consent link
to an address the child controls would be mailing the child a button that
unlocks their own account.
"""

from apps.guardians.constants import GuardianConsentLevel, GuardianConsentMethod
from apps.guardians.models import Guardian, GuardianConsentEvent
from apps.guardians.services.consent_service import request_consent
from apps.guardians.tests.base import (
    FEED_URL,
    GUARDIAN_SHARED_APPROVE_URL,
    PARENT_NAME,
    GuardianTestCase,
    make_minor,
)
from apps.accounts.models import User

CHILD_EMAIL = "sharedchild@example.com"
CHILD_PHONE = "+919876500011"


class DetectionTests(GuardianTestCase):

    def setUp(self):
        super().setUp()
        self.child = self.authenticate(
            make_minor(email=CHILD_EMAIL, username="sharedchild")
        )

    def test_the_childs_own_email_is_detected(self):
        response, token = self.ask_for_consent(parent_email=CHILD_EMAIL)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["mode"], "shared_contact")
        self.assertIsNone(token)

    def test_nothing_is_emailed(self):
        with self.sending_consent_email() as sender:
            self.client.post(
                "/guardian/details",
                {"parent_name": PARENT_NAME, "parent_email": CHILD_EMAIL},
                format="json",
            )

        sender.assert_not_called()

    def test_no_token_is_minted(self):
        # There is no link, so there must be no credential. A hash on this row
        # would be a live token nobody could ever have received.
        self.ask_for_consent(parent_email=CHILD_EMAIL)

        event = GuardianConsentEvent.objects.get(child=self.child)
        self.assertEqual(event.event_type, "requested")
        self.assertEqual(event.token_hash, "")
        self.assertIsNone(event.token_expires_at)

    def test_case_does_not_hide_a_shared_contact(self):
        # "SharedChild@Example.COM" is the same inbox. Missing that would mail
        # the child a link to approve themselves — the exact hole this detects.
        response, token = self.ask_for_consent(
            parent_email=CHILD_EMAIL.upper()
        )

        self.assertEqual(response.data["data"]["mode"], "shared_contact")
        self.assertIsNone(token)

    def test_a_different_address_is_not_shared(self):
        response, token = self.ask_for_consent(parent_email="mum@example.com")

        self.assertEqual(response.data["data"]["mode"], "link_sent")
        self.assertIsNotNone(token)

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

        self.assertEqual(result["mode"], GuardianConsentMethod.SHARED_CONTACT)
        self.assertEqual(result["delivery"], "none")


class InlineApprovalTests(GuardianTestCase):

    def setUp(self):
        super().setUp()
        self.child = self.authenticate(
            make_minor(email=CHILD_EMAIL, username="sharedchild")
        )
        self.ask_for_consent(parent_email=CHILD_EMAIL)

    def _approve(self, **overrides):
        body = {"parent_name": PARENT_NAME, "confirm_18_plus": True}
        body.update(overrides)
        return self.client.post(
            GUARDIAN_SHARED_APPROVE_URL, body, format="json"
        )

    def test_it_unlocks_the_child(self):
        res = self._approve()

        self.assertEqual(res.status_code, 200, res.data)
        self.assert_status(self.child, User.GuardianConsentStatus.APPROVED)
        self.assertEqual(self.client.get(FEED_URL).status_code, 200)

    def test_the_event_records_the_weak_method(self):
        self._approve()

        event = GuardianConsentEvent.objects.get(
            child=self.child, event_type="approved"
        )
        self.assertEqual(event.method, GuardianConsentMethod.SHARED_CONTACT)
        self.assertEqual(event.level, GuardianConsentLevel.ACKNOWLEDGED)
        self.assertEqual(event.notice_version, "2026-10-01")

    def test_the_parents_own_details_are_recorded_separately(self):
        # Guardian.name is what the CHILD said; parent_name_given is what the
        # person answering typed. The two disagreeing is a signal, so they are
        # never collapsed into one column.
        self._approve(parent_name="Priya S Nair", parent_birthdate="1988-04-02")

        event = GuardianConsentEvent.objects.get(
            child=self.child, event_type="approved"
        )
        self.assertEqual(event.parent_name_given, "Priya S Nair")
        self.assertEqual(str(event.parent_birthdate_given), "1988-04-02")
        self.assertEqual(event.guardian.name, PARENT_NAME)

    def test_confirm_18_plus_is_required(self):
        for value in (None, False, "true", 1):
            body = {"parent_name": PARENT_NAME}
            if value is not None:
                body["confirm_18_plus"] = value

            res = self.client.post(
                GUARDIAN_SHARED_APPROVE_URL, body, format="json"
            )

            self.assertEqual(res.status_code, 400, f"confirm={value!r}")
            self.assert_status(self.child, User.GuardianConsentStatus.PENDING)

    def test_approving_twice_writes_one_row(self):
        self._approve()
        self._approve()

        self.assertEqual(
            GuardianConsentEvent.objects.filter(
                child=self.child, event_type="approved"
            ).count(),
            1,
        )

    def test_one_guardian_row_for_the_shared_contact(self):
        self._approve()

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


class InlineApprovalIsRefusedWhenALinkWentOutTests(GuardianTestCase):
    """
    The bypass this guard exists to close.

    Without it a child could name a parent's real address, let the link go out,
    and then approve themselves from their own device — the parent's inbox
    untouched, the row claiming an approval that never happened.
    """

    def setUp(self):
        super().setUp()
        self.child = self.authenticate(
            make_minor(email=CHILD_EMAIL, username="sharedchild")
        )
        _, self.token = self.ask_for_consent(parent_email="mum@example.com")

    def test_inline_approval_is_refused(self):
        res = self.client.post(
            GUARDIAN_SHARED_APPROVE_URL,
            {"parent_name": PARENT_NAME, "confirm_18_plus": True},
            format="json",
        )

        self.assertEqual(res.status_code, 400, res.data)
        self.assertEqual(res.data["data"]["code"], "consent_link_sent")
        self.assert_status(self.child, User.GuardianConsentStatus.PENDING)

    def test_the_honest_route_is_still_open(self):
        # Starting a NEW request with their own contact is how a child moves to
        # the hand-the-phone flow, and it records itself as what it is.
        self.ask_for_consent(parent_email=CHILD_EMAIL)

        res = self.client.post(
            GUARDIAN_SHARED_APPROVE_URL,
            {"parent_name": PARENT_NAME, "confirm_18_plus": True},
            format="json",
        )

        self.assertEqual(res.status_code, 200, res.data)
        event = GuardianConsentEvent.objects.get(
            child=self.child, event_type="approved"
        )
        self.assertEqual(event.method, GuardianConsentMethod.SHARED_CONTACT)
