"""
POST /support/problem-report — the signed-in report.

Grouped by what each test is really protecting:

  PERSISTENCE   the row lands, and the reference is the one thing that comes
                back
  IDENTITY      reported_by is the HUMAN even from a club account — the rule
                the whole service is built around, because a club has no inbox
  VALIDATION    the bounds, and which layer refuses what
  SCREENSHOTS   the ownership check, which is the security case in this file:
                without it a report could reference somebody else's object and
                pull a private image into our admin
  SANITISING    client_context is caller-supplied and lands in a JSONField
  TRIAGE        a link-heavy report is FLAGGED, never refused
  REQUEST META  ip and user agent come off the request, not the body

The load-bearing test here is
``test_a_key_under_another_users_prefix_is_refused``. Everything else can be
re-derived from reading the service; that one is the only thing standing
between a problem report and an arbitrary object key.
"""

import re

from support.models import ProblemReport, ProblemStatus
from support.services.problem_report_service import MAX_DESCRIPTION_LENGTH

from .base import DESCRIPTION, REPORT_URL, SupportTestBase

REFERENCE_RE = re.compile(r"^GZ-[A-Z2-9]{6}$")


class ProblemReportCreateTests(SupportTestBase):

    # =================================================================
    # PERSISTENCE
    # =================================================================

    def test_a_valid_report_persists_and_returns_a_reference(self):
        res = self.client.post(
            REPORT_URL, self._body(), format="json", **self._auth()
        )

        self.assertEqual(res.status_code, 200, res.data)

        reference = res.data["data"]["reference"]
        self.assertRegex(reference, REFERENCE_RE)

        report = ProblemReport.objects.get(reference=reference)
        self.assertEqual(report.category, "media_upload")
        self.assertEqual(report.description, DESCRIPTION)
        self.assertEqual(report.status, ProblemStatus.NEW)

    def test_the_response_carries_the_reference_and_nothing_else(self):
        # A confirmation, not a receipt: no id, no status, no echo of what was
        # submitted. Anything added here is something a client can come to rely
        # on, and this endpoint has nothing else worth publishing.
        res = self.client.post(
            REPORT_URL, self._body(), format="json", **self._auth()
        )

        self.assertEqual(list(res.data["data"].keys()), ["reference"])

    def test_two_reports_from_the_same_user_both_succeed(self):
        # DELIBERATELY no dedup, unlike moderation.Report's twelve partial
        # uniques. Two bugs are two bugs, and the second one filed five minutes
        # later is usually the more useful of the pair.
        first = self.client.post(
            REPORT_URL, self._body(), format="json", **self._auth()
        )
        second = self.client.post(
            REPORT_URL,
            self._body(description="A different screen is broken as well."),
            format="json",
            **self._auth(),
        )

        self.assertEqual(first.status_code, 200, first.data)
        self.assertEqual(second.status_code, 200, second.data)

        self.assertEqual(ProblemReport.objects.filter(reported_by=self.user).count(), 2)
        self.assertNotEqual(
            first.data["data"]["reference"], second.data["data"]["reference"]
        )

    # =================================================================
    # IDENTITY
    # =================================================================

    def test_reported_by_is_the_user_when_acting_personally(self):
        self.client.post(REPORT_URL, self._body(), format="json", **self._auth())

        report = ProblemReport.objects.get()
        self.assertEqual(report.reported_by_id, self.user.id)
        self.assertIsNone(report.acting_org_id)

    def test_reported_by_is_still_the_user_when_acting_as_an_org(self):
        # The rule the service exists to enforce. A bug is experienced by a
        # person and any reply goes to a person — the club is CONTEXT.
        # core.actor.Actor carries user=None for an org actor, so a service
        # reading actor.user here would anonymise every report filed from a
        # club account.
        res = self.client.post(
            REPORT_URL, self._body(), format="json", **self._auth(org=self.org)
        )

        self.assertEqual(res.status_code, 200, res.data)

        report = ProblemReport.objects.get()
        self.assertEqual(report.reported_by_id, self.user.id)
        self.assertEqual(report.acting_org_id, self.org.id)

    # =================================================================
    # VALIDATION
    # =================================================================

    def test_a_description_under_the_minimum_is_a_400(self):
        res = self.client.post(
            REPORT_URL, self._body(description="broken"), format="json", **self._auth()
        )

        self.assertEqual(res.status_code, 400)
        self.assertFalse(ProblemReport.objects.exists())

    def test_a_description_that_is_only_whitespace_padding_is_a_400(self):
        # Measured AFTER stripping. "  hi  " padded to twenty characters is
        # still two characters of report.
        res = self.client.post(
            REPORT_URL,
            self._body(description="   broken    "),
            format="json",
            **self._auth(),
        )

        self.assertEqual(res.status_code, 400)
        self.assertFalse(ProblemReport.objects.exists())

    def test_a_description_over_the_maximum_is_a_400(self):
        res = self.client.post(
            REPORT_URL,
            self._body(description="x" * (MAX_DESCRIPTION_LENGTH + 1)),
            format="json",
            **self._auth(),
        )

        self.assertEqual(res.status_code, 400)
        self.assertFalse(ProblemReport.objects.exists())

    def test_an_unknown_category_is_a_400(self):
        res = self.client.post(
            REPORT_URL,
            self._body(category="harassment"),
            format="json",
            **self._auth(),
        )

        self.assertEqual(res.status_code, 400)
        self.assertFalse(ProblemReport.objects.exists())

    def test_a_missing_category_is_a_400(self):
        res = self.client.post(
            REPORT_URL,
            {"description": DESCRIPTION},
            format="json",
            **self._auth(),
        )

        self.assertEqual(res.status_code, 400)

    def test_filing_requires_a_token(self):
        self.client.force_authenticate(user=None)

        res = self.client.post(REPORT_URL, self._body(), format="json")

        self.assertEqual(res.status_code, 401)
        self.assertFalse(ProblemReport.objects.exists())

    # =================================================================
    # SCREENSHOTS
    # =================================================================

    def test_screenshots_under_the_callers_own_prefix_are_stored(self):
        shots = [
            self._screenshot(name="one.webp"),
            self._screenshot(name="two.png"),
        ]

        res = self.client.post(
            REPORT_URL, self._body(screenshots=shots), format="json", **self._auth()
        )

        self.assertEqual(res.status_code, 200, res.data)

        report = ProblemReport.objects.get()
        # URLs only. The key proves ownership at submit time; the admin renders
        # a plain list of URLs.
        self.assertEqual(report.screenshots, [shots[0]["url"], shots[1]["url"]])

    def test_more_than_three_screenshots_is_a_400(self):
        res = self.client.post(
            REPORT_URL,
            self._body(screenshots=[self._screenshot(name=f"{n}.webp") for n in range(4)]),
            format="json",
            **self._auth(),
        )

        self.assertEqual(res.status_code, 400)
        self.assertFalse(ProblemReport.objects.exists())

    def test_a_key_under_another_users_prefix_is_refused(self):
        """
        THE security case in this file.

        The upload endpoint signs a PUT into the caller's own folder, but the
        create request is a SEPARATE call and a client can send any string it
        likes. Without the re-check, a report could name somebody else's object
        and pull a private image into our admin.
        """
        stolen = self._screenshot(user=self.other)

        res = self.client.post(
            REPORT_URL,
            self._body(screenshots=[stolen]),
            format="json",
            **self._auth(),
        )

        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["message"], "Invalid screenshot")
        self.assertFalse(ProblemReport.objects.exists())

    def test_a_key_under_an_org_the_caller_is_not_acting_as_is_refused(self):
        # The same check on the other half of the dual-actor pair: acting
        # personally, an organizations/ prefix is not the caller's.
        res = self.client.post(
            REPORT_URL,
            self._body(screenshots=[self._screenshot(org=self.org)]),
            format="json",
            **self._auth(),
        )

        self.assertEqual(res.status_code, 400)
        self.assertFalse(ProblemReport.objects.exists())

    def test_an_org_actor_may_attach_from_the_org_prefix(self):
        # And the same call succeeds once the caller IS the org — a guard that
        # refuses everything passes a one-sided test.
        shot = self._screenshot(org=self.org)

        res = self.client.post(
            REPORT_URL,
            self._body(screenshots=[shot]),
            format="json",
            **self._auth(org=self.org),
        )

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(ProblemReport.objects.get().screenshots, [shot["url"]])

    def test_a_url_from_a_foreign_host_is_refused(self):
        res = self.client.post(
            REPORT_URL,
            self._body(
                screenshots=[
                    {
                        "url": "https://evil.example.com/users/x/support/a.webp",
                        "key": f"users/{self.user.id}/support/a.webp",
                    }
                ]
            ),
            format="json",
            **self._auth(),
        )

        self.assertEqual(res.status_code, 400)
        self.assertFalse(ProblemReport.objects.exists())

    # =================================================================
    # SANITISING
    # =================================================================

    def test_client_context_keys_outside_the_allow_list_are_dropped(self):
        self.client.post(
            REPORT_URL,
            self._body(
                client_context={
                    "path": "/highlights/new",
                    "app_version": "1.4.2",
                    # Not on the list. Dropped silently — a client sending
                    # something new is not an error, it just is not stored.
                    "session_token": "super-secret",
                    "cookies": "a=1; b=2",
                }
            ),
            format="json",
            **self._auth(),
        )

        context = ProblemReport.objects.get().client_context

        self.assertEqual(
            context, {"path": "/highlights/new", "app_version": "1.4.2"}
        )

    def test_an_over_long_context_value_is_truncated_not_rejected(self):
        # Truncated, NOT a 400. An unbounded blob is the thing being prevented;
        # a long path is not a reason to lose somebody's bug report.
        self.client.post(
            REPORT_URL,
            self._body(client_context={"path": "/" + ("x" * 500)}),
            format="json",
            **self._auth(),
        )

        context = ProblemReport.objects.get().client_context
        self.assertEqual(len(context["path"]), 200)

    def test_a_nested_context_value_is_coerced_to_a_string(self):
        # `str`, not trusted: a nested object would otherwise be stored whole,
        # and the SIZE of what a caller can write is the entire point.
        self.client.post(
            REPORT_URL,
            self._body(client_context={"viewport": {"w": 1, "h": 2}}),
            format="json",
            **self._auth(),
        )

        self.assertIsInstance(
            ProblemReport.objects.get().client_context["viewport"], str
        )

    def test_a_missing_client_context_stores_an_empty_dict(self):
        self.client.post(REPORT_URL, self._body(), format="json", **self._auth())

        self.assertEqual(ProblemReport.objects.get().client_context, {})

    # =================================================================
    # TRIAGE
    # =================================================================

    def test_a_link_heavy_description_is_flagged_not_rejected(self):
        # Three links is what spam looks like AND what a real report looks like
        # when somebody pastes the pages that break. It saves, in a queue a
        # human filters — a false positive that vanished at the door is worse.
        res = self.client.post(
            REPORT_URL,
            self._body(
                description=(
                    "Broken on http://a.example.com and http://b.example.com "
                    "and also www.c.example.com"
                )
            ),
            format="json",
            **self._auth(),
        )

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(
            ProblemReport.objects.get().status, ProblemStatus.SPAM_SUSPECT
        )

    def test_two_links_are_an_ordinary_report(self):
        self.client.post(
            REPORT_URL,
            self._body(
                description=(
                    "It breaks on http://a.example.com and http://b.example.com"
                )
            ),
            format="json",
            **self._auth(),
        )

        self.assertEqual(ProblemReport.objects.get().status, ProblemStatus.NEW)

    # =================================================================
    # REQUEST META
    # =================================================================

    def test_ip_and_user_agent_are_captured_from_the_request(self):
        headers = self._auth()

        self.client.post(
            REPORT_URL,
            self._body(),
            format="json",
            HTTP_USER_AGENT="Mozilla/5.0 (Linux; Android 14) GoatzaTest",
            HTTP_X_FORWARDED_FOR="203.0.113.7, 70.41.3.18",
            **headers,
        )

        report = ProblemReport.objects.get()
        # The LEFT-most forwarded entry — the rest are the proxies it passed
        # through, and on Render REMOTE_ADDR is the proxy for everybody.
        self.assertEqual(report.ip_address, "203.0.113.7")
        self.assertEqual(
            report.user_agent, "Mozilla/5.0 (Linux; Android 14) GoatzaTest"
        )

    def test_a_forged_ip_header_does_not_break_the_write(self):
        # X-Forwarded-For is attacker-controlled and lands in an inet column.
        # Unvalidated, a junk header is not a bad audit row — it is an
        # exception thrown inside the write, i.e. a header that stops people
        # reporting bugs.
        res = self.client.post(
            REPORT_URL,
            self._body(),
            format="json",
            HTTP_X_FORWARDED_FOR="'; drop table problem_reports; --",
            **self._auth(),
        )

        self.assertEqual(res.status_code, 200, res.data)
        self.assertIsNone(ProblemReport.objects.get().ip_address)
