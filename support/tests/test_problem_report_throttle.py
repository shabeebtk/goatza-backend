"""
The two budgets, and the shape of the refusal.

Three things are being protected here, and only one of them is the number:

  * THE ENVELOPE. DRF raises ``Throttled`` from ``initial()``, before the view
    body runs, so a try/except in ``post()`` never sees it and the default
    handler answers with a bare ``{"detail": …}`` — the one response shape no
    client parser in this app expects. ``handle_exception`` is what keeps the
    429 in ``response_data``, and this file is what proves it still does.

  * THE KEY on the authenticated bucket. It is per USER, not per actor. An
    actor-scoped bucket would hand one person a fresh five for every club they
    belong to, and that is the regression
    ``test_switching_to_an_org_actor_does_not_reset_the_bucket`` exists to
    catch. Nothing about the code makes that obvious — ``UserRateThrottle``
    keying on ``request.user`` is one word away from keying on the actor.

  * THE KEY on the public bucket: per IP, and independent between addresses.
    One connection burning its three must not lock out the next visitor.

``cache.clear()`` in setUp is mandatory: DRF's counters live in the shared
cache and outlive a test.
"""

from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from support.models import ProblemReport

from .base import DESCRIPTION, PUBLIC_REPORT_URL, REPORT_URL, SupportTestBase

# One over the rate, so the last call in each list is the refused one.
AUTHENTICATED_CALLS = 6   # support_report        = 5/hour
PUBLIC_CALLS = 4          # support_report_public = 3/hour


class ProblemReportThrottleTests(SupportTestBase):
    """The authenticated bucket: 5/hour, per user."""

    def _file(self, **headers):
        return self.client.post(
            REPORT_URL,
            self._body(description=DESCRIPTION),
            format="json",
            **headers,
        )

    def test_the_sixth_report_in_an_hour_is_refused(self):
        headers = self._auth()

        statuses = [self._file(**headers).status_code for _ in range(AUTHENTICATED_CALLS)]

        self.assertEqual(statuses[:5], [200] * 5)
        self.assertEqual(statuses[5], 429)

        # The five that got through are still there. A throttle protects the
        # budget, it does not undo what was already filed.
        self.assertEqual(ProblemReport.objects.count(), 5)

    def test_the_429_uses_the_standard_envelope_with_retry_after(self):
        headers = self._auth()

        for _ in range(AUTHENTICATED_CALLS - 1):
            self._file(**headers)

        res = self._file(**headers)

        self.assertEqual(res.status_code, 429)

        body = res.json()
        # NOT DRF's bare {"detail": …}. Every client in this app reads
        # success/message/data, and a 429 that breaks that shape is a 429 that
        # renders as "Something went wrong" with no wait time.
        self.assertNotIn("detail", body)
        self.assertFalse(body["success"])
        self.assertEqual(body["message"], "Too many reports. Please try again later.")
        self.assertEqual(
            body["data"]["errors"]["non_field_errors"],
            "Too many reports. Please try again later.",
        )

        # The one thing that makes the refusal actionable.
        self.assertIn("retry_after", body["data"])
        self.assertGreater(body["data"]["retry_after"], 0)

    def test_switching_to_an_org_actor_does_not_reset_the_bucket(self):
        """
        THE regression in this file.

        The budget belongs to the HUMAN behind the headers. An actor-scoped
        bucket would give one person a fresh five for every org they are a
        member of, which is exactly the leverage a script wants — and it would
        look correct in every other test here, because every other test uses
        one actor.
        """
        personal = self._auth()

        for _ in range(5):
            self.assertEqual(self._file(**personal).status_code, 200)

        # Same human, different hat, same X-Actor headers the app really sends.
        as_org = self._auth(org=self.org)

        self.assertEqual(self._file(**as_org).status_code, 429)
        self.assertEqual(ProblemReport.objects.count(), 5)

    def test_another_user_has_their_own_budget(self):
        # The other half of the same rule: per user means PER user. A throttle
        # that refused everybody once one person hit the limit would pass the
        # test above on its own.
        headers = self._auth()
        for _ in range(AUTHENTICATED_CALLS):
            self._file(**headers)

        res = self._file(**self._auth(user=self.other))

        self.assertEqual(res.status_code, 200, res.data)


class PublicProblemReportThrottleTests(TestCase):
    """The anonymous bucket: 3/hour, per IP."""

    def setUp(self):
        cache.clear()
        self.client = APIClient()

    def _file(self, ip="203.0.113.5"):
        return self.client.post(
            PUBLIC_REPORT_URL,
            {
                "category": "account_login",
                "description": DESCRIPTION,
                "contact_email": "someone@example.com",
            },
            format="json",
            REMOTE_ADDR=ip,
        )

    def test_the_fourth_report_from_one_ip_in_an_hour_is_refused(self):
        statuses = [self._file().status_code for _ in range(PUBLIC_CALLS)]

        self.assertEqual(statuses[:3], [200] * 3)
        self.assertEqual(statuses[3], 429)
        self.assertEqual(ProblemReport.objects.count(), 3)

    def test_the_public_429_uses_the_standard_envelope_too(self):
        for _ in range(PUBLIC_CALLS - 1):
            self._file()

        body = self._file().json()

        self.assertNotIn("detail", body)
        self.assertFalse(body["success"])
        self.assertIn("retry_after", body["data"])

    def test_a_different_ip_has_its_own_budget(self):
        for _ in range(PUBLIC_CALLS):
            self._file(ip="203.0.113.5")

        res = self._file(ip="198.51.100.9")

        self.assertEqual(res.status_code, 200, res.data)

    def test_the_public_budget_is_tighter_than_the_authenticated_one(self):
        # Not a behaviour so much as the reason the two scopes exist at all: a
        # single rate shared by both would either be too loose for the open
        # internet or too tight for somebody signed in.
        from support.throttles import (
            ProblemReportThrottle,
            PublicProblemReportThrottle,
        )

        self.assertEqual(ProblemReportThrottle.scope, "support_report")
        self.assertEqual(
            PublicProblemReportThrottle.scope, "support_report_public"
        )
        self.assertLess(
            PublicProblemReportThrottle().num_requests,
            ProblemReportThrottle().num_requests,
        )
