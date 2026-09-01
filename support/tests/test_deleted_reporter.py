"""
What happens to a problem report when its reporter is deleted.

THIS FILE IS THE GUARD ON A MISSING CONSTRAINT.

``ProblemReport`` has no CHECK constraints, and the tempting one is "reported_by
is null implies contact_email is set" — an anonymous report nobody can reply to
is close to useless. It is still wrong at the database layer, and this is why:

  ``reported_by`` is SET_NULL, so hard-deleting a user turns an old
  AUTHENTICATED report into an anonymous one, and that report correctly has a
  blank contact_email because the account carried the address. The constraint
  would reject that UPDATE — and a report would end up blocking its own
  reporter's account deletion.

Same failure mode ``moderation.Report.report_at_most_one_target`` documents.
If somebody adds that constraint later, ``test_a_reporter_with_a_report_can_be
_hard_deleted`` is the test that fails, and the docstring above is the reason.

The "anonymous reports need an email" rule lives in the PUBLIC SERIALIZER
instead — the only layer that can tell "filed with no account" apart from "the
account was deleted a year later". ``test_problem_report_public`` covers that
half.
"""

from support.models import ProblemReport, ProblemStatus

from .base import REPORT_URL, SupportTestBase


class DeletedReporterTests(SupportTestBase):

    def _file_a_report(self, **overrides):
        res = self.client.post(
            REPORT_URL, self._body(**overrides), format="json", **self._auth()
        )
        self.assertEqual(res.status_code, 200, res.data)
        return ProblemReport.objects.get(reference=res.data["data"]["reference"])

    def test_a_reporter_with_a_report_can_be_hard_deleted(self):
        report = self._file_a_report()

        self.assertEqual(report.reported_by_id, self.user.id)
        # An authenticated report carries no contact email — the account had
        # the address. This is precisely the row a CHECK constraint would
        # later refuse to update.
        self.assertEqual(report.contact_email, "")

        self.user.delete()

        report.refresh_from_db()
        self.assertIsNone(report.reported_by_id)
        self.assertEqual(report.contact_email, "")

    def test_the_report_survives_the_deletion_intact(self):
        # SET_NULL, not CASCADE. The crash the report describes is still there
        # after the reporter leaves, and the report is the only record of it.
        report = self._file_a_report(
            description="Highlights upload fails at 80% every single time.",
            client_context={"path": "/highlights/new"},
        )
        reference = report.reference

        self.user.delete()

        survivor = ProblemReport.objects.get(reference=reference)
        self.assertEqual(
            survivor.description,
            "Highlights upload fails at 80% every single time.",
        )
        self.assertEqual(survivor.client_context, {"path": "/highlights/new"})
        self.assertEqual(survivor.status, ProblemStatus.NEW)
        self.assertEqual(ProblemReport.objects.count(), 1)

    def test_an_org_reports_acting_org_survives_the_orgs_deletion(self):
        # The same rule on the other FK: acting_org is context, not ownership,
        # and a deleted club must not take the bug with it.
        res = self.client.post(
            REPORT_URL, self._body(), format="json", **self._auth(org=self.org)
        )
        report = ProblemReport.objects.get(reference=res.data["data"]["reference"])
        self.assertEqual(report.acting_org_id, self.org.id)

        self.org.delete()

        report.refresh_from_db()
        self.assertIsNone(report.acting_org_id)
        self.assertEqual(report.reported_by_id, self.user.id)

    def test_a_resolver_can_be_deleted_without_taking_the_report(self):
        # resolved_by is SET_NULL too, and for the same reason: a member of
        # staff leaving must not delete the history of what they resolved.
        report = self._file_a_report()
        report.resolved_by = self.other
        report.status = ProblemStatus.RESOLVED
        report.save(update_fields=["resolved_by", "status"])

        self.other.delete()

        report.refresh_from_db()
        self.assertIsNone(report.resolved_by_id)
        self.assertEqual(report.status, ProblemStatus.RESOLVED)

    def test_the_model_declares_no_check_constraints(self):
        """
        Asserted directly, so adding one is a deliberate act with a failing
        test attached rather than a quiet migration.
        """
        from django.db.models import CheckConstraint

        checks = [
            constraint
            for constraint in ProblemReport._meta.constraints
            if isinstance(constraint, CheckConstraint)
        ]

        self.assertEqual(checks, [], "see this module's docstring")
