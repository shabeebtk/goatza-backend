"""
POST /public/support/problem-report — the logged-out report.

This route exists because the screens most likely to be broken are the ones
somebody reaches BEFORE they have a session: login, signup, OTP. Everything
tested here follows from that one fact.

Two things are load-bearing:

  * ``test_a_tripped_honeypot_writes_nothing`` asserts the ROW COUNT, not just
    the status code. A honeypot that answers 200 and still saves the row is a
    honeypot that does nothing at all, and the status code alone cannot tell
    the two apart.
  * ``test_a_missing_contact_email_is_a_400`` is the serializer, deliberately
    NOT a database constraint. See ``test_deleted_reporter`` for the case a
    constraint would break.
"""

from django.conf import settings
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from support.models import ProblemReport, ProblemStatus

from .base import DESCRIPTION, PUBLIC_REPORT_URL

EMAIL = "someone@example.com"


class PublicProblemReportTests(TestCase):

    def setUp(self):
        # The endpoint is throttled at 3/hour per IP and DRF keeps its counters
        # in the shared cache, which outlives a test.
        cache.clear()
        self.client = APIClient()

    def _body(self, **overrides):
        body = {
            "category": "account_login",
            "description": DESCRIPTION,
            "contact_email": EMAIL,
        }
        body.update(overrides)
        return body

    # =================================================================
    # THE HAPPY PATH
    # =================================================================

    def test_a_valid_anonymous_report_persists(self):
        res = self.client.post(PUBLIC_REPORT_URL, self._body(), format="json")

        self.assertEqual(res.status_code, 200, res.data)

        report = ProblemReport.objects.get(
            reference=res.data["data"]["reference"]
        )
        self.assertIsNone(report.reported_by_id)
        self.assertIsNone(report.acting_org_id)
        self.assertEqual(report.contact_email, EMAIL)
        self.assertEqual(report.description, DESCRIPTION)
        self.assertEqual(report.status, ProblemStatus.NEW)

    def test_the_endpoint_is_reachable_with_no_authorization_header(self):
        # The whole point. A brand-new client with no token, no cookie and no
        # actor headers must reach this — anything that makes it 401 makes the
        # feature pointless.
        anonymous = APIClient()

        res = anonymous.post(PUBLIC_REPORT_URL, self._body(), format="json")

        self.assertEqual(res.status_code, 200, res.data)
        self.assertNotIn("HTTP_AUTHORIZATION", anonymous._credentials)
        self.assertEqual(ProblemReport.objects.count(), 1)

    def test_the_response_carries_the_reference_and_nothing_else(self):
        res = self.client.post(PUBLIC_REPORT_URL, self._body(), format="json")

        self.assertEqual(list(res.data["data"].keys()), ["reference"])

    def test_client_context_is_allow_listed_here_too(self):
        # Sharper than on the authenticated route: this blob arrives from an
        # UNAUTHENTICATED caller and lands in a JSONField.
        self.client.post(
            PUBLIC_REPORT_URL,
            self._body(
                client_context={"path": "/auth", "session_token": "secret"}
            ),
            format="json",
        )

        self.assertEqual(
            ProblemReport.objects.get().client_context, {"path": "/auth"}
        )

    # =================================================================
    # CONTACT EMAIL
    # =================================================================

    def test_a_missing_contact_email_is_a_400(self):
        # Required by the SERIALIZER, not by the database. An anonymous report
        # nobody can reply to is close to useless, but a column-level rule
        # would fail the day somebody deletes their account — see
        # test_deleted_reporter.
        res = self.client.post(
            PUBLIC_REPORT_URL,
            {"category": "account_login", "description": DESCRIPTION},
            format="json",
        )

        self.assertEqual(res.status_code, 400)
        self.assertFalse(ProblemReport.objects.exists())

    def test_a_blank_contact_email_is_a_400(self):
        res = self.client.post(
            PUBLIC_REPORT_URL, self._body(contact_email=""), format="json"
        )

        self.assertEqual(res.status_code, 400)
        self.assertFalse(ProblemReport.objects.exists())

    def test_a_malformed_contact_email_is_a_400(self):
        res = self.client.post(
            PUBLIC_REPORT_URL, self._body(contact_email="not-an-email"), format="json"
        )

        self.assertEqual(res.status_code, 400)
        self.assertFalse(ProblemReport.objects.exists())

    # =================================================================
    # SCREENSHOTS
    # =================================================================

    def test_screenshots_are_refused_on_the_public_route(self):
        """
        Logged-out reports are TEXT ONLY.

        A presigned PUT handed to an anonymous caller is a write path into the
        bucket from the open internet. The public serializer has no such field,
        so one sent anyway is ignored and nothing is stored.
        """
        # Built from the REAL media base, so this is a screenshot that would
        # have been perfectly valid on the authenticated route. The point is
        # that the public serializer has no field for it, not that the URL
        # happens to fail a source check.
        key = "users/00000000-0000-0000-0000-000000000000/support/a.webp"

        res = self.client.post(
            PUBLIC_REPORT_URL,
            self._body(
                screenshots=[
                    {"url": f"{settings.MEDIA_PUBLIC_BASE_URL}/{key}", "key": key}
                ]
            ),
            format="json",
        )

        # The report itself still lands — the text is the report, and refusing
        # it over an ignored field would lose a bug for no gain.
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(ProblemReport.objects.get().screenshots, [])

    # =================================================================
    # HONEYPOT
    # =================================================================

    def test_a_tripped_honeypot_writes_nothing(self):
        """
        The one that matters.

        A bot that filled the hidden field gets a response indistinguishable
        from a real one — 200, the same single key, a code from the same
        generator — and NOTHING is persisted. Asserting only the status code
        would pass just as happily against a honeypot that saved the row.
        """
        before = ProblemReport.objects.count()

        res = self.client.post(
            PUBLIC_REPORT_URL,
            self._body(website="http://spam.example.com"),
            format="json",
        )

        self.assertEqual(res.status_code, 200, res.data)
        self.assertRegex(res.data["data"]["reference"], r"^GZ-[A-Z2-9]{6}$")
        self.assertEqual(ProblemReport.objects.count(), before)

    def test_a_decoy_reference_matches_no_row(self):
        res = self.client.post(
            PUBLIC_REPORT_URL, self._body(website="anything"), format="json"
        )

        self.assertFalse(
            ProblemReport.objects.filter(
                reference=res.data["data"]["reference"]
            ).exists()
        )

    def test_a_caught_bot_cannot_tell_it_was_caught(self):
        # Byte-for-byte the same envelope shape as a real submission: same
        # status, same success flag, same message, same keys. A single
        # difference here is the tell that teaches whoever wrote the bot to
        # stop filling the field in.
        real = self.client.post(PUBLIC_REPORT_URL, self._body(), format="json")

        cache.clear()  # the two submissions share an IP bucket
        decoy = self.client.post(
            PUBLIC_REPORT_URL, self._body(website="caught"), format="json"
        )

        self.assertEqual(real.status_code, decoy.status_code)
        self.assertEqual(real.data["success"], decoy.data["success"])
        self.assertEqual(real.data["message"], decoy.data["message"])
        self.assertEqual(list(real.data["data"]), list(decoy.data["data"]))

    def test_an_empty_honeypot_is_an_ordinary_submission(self):
        # Every human sends this field blank — a form that treated "present but
        # empty" as caught would reject everybody.
        res = self.client.post(
            PUBLIC_REPORT_URL, self._body(website=""), format="json"
        )

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(ProblemReport.objects.count(), 1)

    # =================================================================
    # VALIDATION
    # =================================================================

    def test_a_short_description_is_a_400(self):
        res = self.client.post(
            PUBLIC_REPORT_URL, self._body(description="broken"), format="json"
        )

        self.assertEqual(res.status_code, 400)
        self.assertFalse(ProblemReport.objects.exists())

    def test_an_unknown_category_is_a_400(self):
        res = self.client.post(
            PUBLIC_REPORT_URL, self._body(category="harassment"), format="json"
        )

        self.assertEqual(res.status_code, 400)
        self.assertFalse(ProblemReport.objects.exists())

    def test_a_link_heavy_anonymous_report_is_flagged_not_rejected(self):
        res = self.client.post(
            PUBLIC_REPORT_URL,
            self._body(
                description=(
                    "See http://a.example.com http://b.example.com "
                    "http://c.example.com for the broken pages"
                )
            ),
            format="json",
        )

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(
            ProblemReport.objects.get().status, ProblemStatus.SPAM_SUSPECT
        )
