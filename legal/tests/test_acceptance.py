"""
Legal consent: the audit trail, and the gate that reads its cache.

Every test here is really about one of two invariants:

  * the append-only table and the denormalized columns on User never disagree
  * what makes a document pending is the CURRENT version, read at call time

The version bump tests therefore patch ``LEGAL_DOCUMENTS`` rather than editing
the constants — the registry is a deployment fact, and a test that changed it
for real would make every other test in the suite depend on the order it ran in.
"""

from unittest.mock import patch

from django.test import TestCase

from accounts.models import User, UserProfile
from legal.constants import (
    LEGAL_DOCUMENTS,
    PRIVACY_VERSION,
    REQUIRED_DOCUMENTS,
    TERMS_VERSION,
)
from legal.models import LegalAcceptance
from legal.selectors.acceptance_selectors import get_pending_documents, has_accepted
from legal.services.acceptance_service import record_acceptance


def make_user(email="player@example.com"):
    user = User.objects.create_user(email=email, password="password123")
    UserProfile.objects.create(user=user, name="Player")
    return user


def bumped_registry(**versions):
    """
    A copy of the registry with some versions replaced. Used with ``patch`` to
    simulate a deploy that published new text.
    """
    registry = {key: dict(value) for key, value in LEGAL_DOCUMENTS.items()}
    for document, version in versions.items():
        registry[document]["version"] = version
    return registry


class RegistryTests(TestCase):

    def test_terms_and_privacy_are_the_gating_documents(self):
        self.assertEqual(REQUIRED_DOCUMENTS, ("terms", "privacy"))

    def test_published_but_non_gating_documents_never_block(self):
        for document in ("guidelines", "safety"):
            self.assertIn(document, LEGAL_DOCUMENTS, document)
            self.assertFalse(
                LEGAL_DOCUMENTS[document]["requires_acceptance"], document
            )


class FirstAcceptanceTests(TestCase):

    def setUp(self):
        self.user = make_user()

    def test_a_new_user_is_pending_on_everything_that_gates(self):
        self.assertEqual(get_pending_documents(self.user), ["terms", "privacy"])
        self.assertIsNone(self.user.terms_version)
        self.assertIsNone(self.user.terms_accepted_at)

    def test_accepting_writes_the_audit_row_and_the_cached_columns(self):
        record_acceptance(
            user=self.user,
            documents=["terms", "privacy"],
            ip_address="203.0.113.7",
            user_agent="Mozilla/5.0",
        )

        rows = LegalAcceptance.objects.filter(user=self.user)
        self.assertEqual(rows.count(), 2)

        terms = rows.get(document="terms")
        self.assertEqual(terms.version, TERMS_VERSION)
        self.assertEqual(terms.ip_address, "203.0.113.7")
        self.assertEqual(terms.user_agent, "Mozilla/5.0")

        self.user.refresh_from_db()
        self.assertEqual(self.user.terms_version, TERMS_VERSION)
        self.assertEqual(self.user.privacy_version, PRIVACY_VERSION)
        # The cached timestamp is the row's, not a second call to now().
        self.assertEqual(self.user.terms_accepted_at, terms.accepted_at)

    def test_accepting_clears_the_gate(self):
        record_acceptance(user=self.user, documents=["terms", "privacy"])

        self.user.refresh_from_db()
        self.assertEqual(get_pending_documents(self.user), [])
        self.assertTrue(has_accepted(self.user, "terms"))

    def test_a_partial_acceptance_leaves_the_rest_pending(self):
        record_acceptance(user=self.user, documents=["terms"])

        self.user.refresh_from_db()
        self.assertEqual(get_pending_documents(self.user), ["privacy"])

    def test_a_non_gating_document_is_recorded_but_changes_no_columns(self):
        record_acceptance(user=self.user, documents=["guidelines"])

        self.assertTrue(
            LegalAcceptance.objects.filter(
                user=self.user, document="guidelines"
            ).exists()
        )

        self.user.refresh_from_db()
        # Still pending on the two that actually gate.
        self.assertEqual(get_pending_documents(self.user), ["terms", "privacy"])

    def test_an_over_long_user_agent_is_truncated_not_rejected(self):
        record_acceptance(
            user=self.user, documents=["terms"], user_agent="u" * 900
        )

        row = LegalAcceptance.objects.get(user=self.user, document="terms")
        self.assertEqual(len(row.user_agent), 500)


class IdempotencyTests(TestCase):

    def setUp(self):
        self.user = make_user()

    def test_recording_the_same_acceptance_twice_writes_one_row(self):
        record_acceptance(user=self.user, documents=["terms"])
        record_acceptance(user=self.user, documents=["terms"])

        self.assertEqual(
            LegalAcceptance.objects.filter(
                user=self.user, document="terms"
            ).count(),
            1,
        )

    def test_a_repeat_acceptance_keeps_the_original_moment(self):
        record_acceptance(
            user=self.user, documents=["terms"], ip_address="203.0.113.7"
        )
        original = LegalAcceptance.objects.get(user=self.user, document="terms")

        record_acceptance(
            user=self.user, documents=["terms"], ip_address="198.51.100.4"
        )
        again = LegalAcceptance.objects.get(user=self.user, document="terms")

        # The row is the evidence of when they agreed, not of when we last
        # asked — neither the timestamp nor the captured context moves.
        self.assertEqual(again.accepted_at, original.accepted_at)
        self.assertEqual(again.ip_address, "203.0.113.7")

        self.user.refresh_from_db()
        self.assertEqual(self.user.terms_accepted_at, original.accepted_at)

    def test_a_duplicated_key_in_one_call_is_one_row(self):
        acceptances = record_acceptance(
            user=self.user, documents=["terms", "terms"]
        )

        self.assertEqual(len(acceptances), 1)
        self.assertEqual(LegalAcceptance.objects.filter(user=self.user).count(), 1)


class VersionBumpTests(TestCase):

    def setUp(self):
        self.user = make_user()
        record_acceptance(user=self.user, documents=["terms", "privacy"])
        self.user.refresh_from_db()

    def test_a_bump_makes_only_that_document_pending_again(self):
        with patch(
            "legal.constants.LEGAL_DOCUMENTS", bumped_registry(terms="2027-01-01")
        ):
            self.assertEqual(get_pending_documents(self.user), ["terms"])

    def test_accepting_the_new_version_keeps_the_old_row(self):
        bumped = bumped_registry(terms="2027-01-01")

        with patch("legal.constants.LEGAL_DOCUMENTS", bumped):
            record_acceptance(user=self.user, documents=["terms"])

            self.user.refresh_from_db()
            self.assertEqual(get_pending_documents(self.user), [])
            self.assertEqual(self.user.terms_version, "2027-01-01")

        # Both versions survive — superseding is not deleting.
        versions = set(
            LegalAcceptance.objects
            .filter(user=self.user, document="terms")
            .values_list("version", flat=True)
        )
        self.assertEqual(versions, {TERMS_VERSION, "2027-01-01"})

    def test_a_rolled_back_version_puts_the_user_back_in_the_gate(self):
        # The user holds 2026-10-01; the registry now serves an earlier text.
        # "Not the current version" is pending, in both directions.
        with patch(
            "legal.constants.LEGAL_DOCUMENTS", bumped_registry(terms="2026-01-01")
        ):
            self.assertEqual(get_pending_documents(self.user), ["terms"])


class ValidationTests(TestCase):

    def setUp(self):
        self.user = make_user()

    def test_an_unknown_document_is_rejected(self):
        with self.assertRaises(ValueError):
            record_acceptance(user=self.user, documents=["cookies"])

    def test_an_unknown_document_writes_nothing_at_all(self):
        # Validation runs over the whole list before the transaction opens, so
        # a payload with one good key and one typo records neither.
        with self.assertRaises(ValueError):
            record_acceptance(user=self.user, documents=["terms", "cookies"])

        self.assertFalse(LegalAcceptance.objects.filter(user=self.user).exists())

        self.user.refresh_from_db()
        self.assertIsNone(self.user.terms_version)
        self.assertEqual(get_pending_documents(self.user), ["terms", "privacy"])

    def test_an_empty_document_list_is_rejected(self):
        with self.assertRaises(ValueError):
            record_acceptance(user=self.user, documents=[])
