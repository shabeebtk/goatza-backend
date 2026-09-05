"""Recruitment emails: the tier decision, the rollup counts, and the triggers.

The throttle is the part worth testing hardest. Its whole job is to be quiet
without losing anything — an application that arrives during a gap gets no mail
of its own but MUST turn up in the next alert's count, and two people applying
at once must produce one alert, not two. Those are counting bugs, and counting
bugs are invisible in production until an org complains it was never told.

The decision function is exercised directly with injected clocks (no DB, no
freezing), and the wiring is exercised through ApplicationService so the counts
under test are the ones the real query produces.
"""

from datetime import datetime, timedelta, timezone as dt_timezone
from unittest.mock import patch

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from rest_framework.test import APITestCase

from accounts.models import User, UserProfile
from core.actor import Actor
from legal.testing import accept_current_terms
from organization.models import Organization, OrganizationMember
from sports.models import Sport, SportPosition
from recruitments.models import Recruitment, RecruitmentApplication
from recruitments.services.application_service import ApplicationService
from recruitments.services.applicant_alert_service import (
    should_send_applicant_alert,
)
from utils.transactional_emails import (
    new_applicant_alert_recipients,
    send_application_status_email,
)

# The shipped table, restated so a test failure says which rule broke rather
# than which settings value moved.
TIERS = [(3, 0), (7, 3600), (None, 14400)]

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=dt_timezone.utc)


def _ago(**kwargs):
    """A moment `kwargs` before NOW — reads as "the last alert was N ago"."""
    return NOW - timedelta(**kwargs)


class ApplicantAlertTierTests(SimpleTestCase):
    """The pure decision, one rule per test."""

    def _decide(self, total_count, last_alert_at):
        return should_send_applicant_alert(
            total_count=total_count,
            last_alert_at=last_alert_at,
            now=NOW,
            tiers=TIERS,
        )

    # ---------------- tier 1: the first three, instantly ----------------

    def test_the_first_three_applicants_always_alert(self):
        for count in (1, 2, 3):
            with self.subTest(total_count=count):
                self.assertTrue(self._decide(count, None))

    def test_tier_one_alerts_again_immediately_after_the_last_one(self):
        # A zero-second gap means back-to-back alerts are fine here: three
        # emails is the point, an org wants to know its first applicants.
        for count in (1, 2, 3):
            with self.subTest(total_count=count):
                self.assertTrue(self._decide(count, _ago(seconds=1)))

    # ---------------- tier 2: hourly ----------------

    def test_the_fourth_applicant_ten_minutes_later_is_held(self):
        self.assertFalse(self._decide(4, _ago(minutes=10)))

    def test_the_fourth_applicant_sixty_one_minutes_later_alerts(self):
        self.assertTrue(self._decide(4, _ago(minutes=61)))

    # ---------------- tier 3: four-hourly ----------------

    def test_the_eighth_applicant_two_hours_later_is_held(self):
        self.assertFalse(self._decide(8, _ago(hours=2)))

    def test_the_eighth_applicant_five_hours_later_alerts(self):
        self.assertTrue(self._decide(8, _ago(hours=5)))

    # ---------------- the boundary between them ----------------

    def test_seven_is_the_last_hourly_count_and_eight_is_the_first_four_hourly(self):
        # An hour and a minute after the last alert: 7 is still on the hourly
        # tier and goes, 8 has crossed onto the four-hour tier and waits.
        self.assertTrue(self._decide(7, _ago(minutes=61)))
        self.assertFalse(self._decide(8, _ago(minutes=61)))

    def test_a_gap_exactly_equal_to_the_tier_passes(self):
        # ">= gap", not "> gap" — a mail held back by one second would be a
        # confusing thing to explain.
        self.assertTrue(self._decide(4, _ago(hours=1)))
        self.assertTrue(self._decide(8, _ago(hours=4)))

    def test_a_never_alerted_recruitment_always_passes(self):
        # However many applications it has: nobody has been told anything yet.
        self.assertTrue(self._decide(500, None))

    def test_a_table_with_no_matching_tier_stays_quiet(self):
        # Misconfiguration (no unbounded catch-all) costs a missed alert, not
        # an uncapped one.
        self.assertFalse(
            should_send_applicant_alert(
                total_count=99, last_alert_at=None, now=NOW, tiers=[(3, 0)]
            )
        )


class RecruitmentEmailFixture(APITestCase):
    """Shared org / recruitment / applicants setup."""

    def setUp(self):
        cache.clear()

        self.owner = User.objects.create_user(
            email="owner@example.com", password="pass1234", username="clubowner"
        )
        accept_current_terms(self.owner)

        self.org = Organization.objects.create(
            name="Trivandrum City FC",
            username="trivandrumcityfc",
            type=Organization.Type.CLUB,
        )
        OrganizationMember.objects.create(
            organization=self.org,
            user=self.owner,
            role=OrganizationMember.Role.OWNER,
        )

        self.sport = Sport.objects.create(name="Football", icon_name="mdi:soccer")
        self.position = SportPosition.objects.create(
            sport=self.sport, name="Striker"
        )

        self.recruitment = Recruitment.objects.create(
            organization=self.org,
            sport=self.sport,
            status=Recruitment.Status.ACTIVE,
            title="U-19 Striker Trials",
            recruitment_type="open_trial",
            apply_method="goatza",
            visibility=Recruitment.Visibility.PUBLIC,
            location_name="Thiruvananthapuram, Kerala",
        )

    def _player(self, index):
        player = User.objects.create_user(
            email=f"player{index}@example.com",
            password="pass1234",
            username=f"player{index}",
        )
        accept_current_terms(player)
        UserProfile.objects.create(user=player, name=f"Player {index}")
        return player

    def _apply(self, player):
        """Run a real apply(), executing the post-commit callbacks."""
        with self.captureOnCommitCallbacks(execute=True):
            return ApplicationService.apply(
                Actor(actor_type="user", user=player),
                self.recruitment.id,
                {
                    "shared_name": f"Player {player.username}",
                    "shared_phone": "+919876543210",
                    "shared_email": player.email,
                },
            )

    def _reload(self):
        self.recruitment.refresh_from_db()
        return self.recruitment


@patch("recruitments.services.application_service.send_new_applicant_alert_email")
@patch("recruitments.services.application_service.send_application_received_email")
class ApplyEmailWiringTests(RecruitmentEmailFixture):
    """What apply() sends, and what it stamps."""

    def test_each_apply_sends_exactly_one_received_email(
        self, mock_received, _mock_alert
    ):
        application = self._apply(self._player(1))

        self.assertEqual(mock_received.call_count, 1)
        self.assertEqual(
            mock_received.call_args.kwargs, {"application": application}
        )

    def test_the_first_applicant_alerts_and_stamps_the_recruitment(
        self, _mock_received, mock_alert
    ):
        self.assertIsNone(self.recruitment.last_applicant_alert_at)

        application = self._apply(self._player(1))

        self.assertEqual(mock_alert.call_count, 1)
        kwargs = mock_alert.call_args.kwargs
        self.assertEqual(kwargs["recruitment"], self.recruitment)
        self.assertEqual(kwargs["latest_application"], application)
        self.assertEqual(kwargs["new_count"], 1)
        self.assertEqual(kwargs["total_count"], 1)

        self.assertIsNotNone(self._reload().last_applicant_alert_at)

    @override_settings(APPLICANT_ALERT_TIERS=[(1, 0), (None, 3600)])
    def test_a_held_alert_leaves_the_stamp_alone(
        self, _mock_received, mock_alert
    ):
        self._apply(self._player(1))          # alerts, stamps
        stamped_at = self._reload().last_applicant_alert_at

        mock_alert.reset_mock()
        self._apply(self._player(2))          # inside the hour: silent

        mock_alert.assert_not_called()
        # The stamp must NOT advance on a held application, or the gap would
        # restart on every apply and the next alert would never fire.
        self.assertEqual(self._reload().last_applicant_alert_at, stamped_at)

    @override_settings(APPLICANT_ALERT_TIERS=[(1, 0), (None, 3600)])
    def test_applications_held_during_a_gap_are_counted_into_the_next_alert(
        self, _mock_received, mock_alert
    ):
        # One alerted application, then three that arrive while the hourly gap
        # is closed, then a fourth once it reopens. The rollup has to speak for
        # all four of the quiet ones — that is the whole promise of batching.
        first = self._apply(self._player(1))
        for index in (2, 3, 4):
            self._apply(self._player(index))

        self.assertEqual(mock_alert.call_count, 1)  # only the first one went

        # Age everything so the gap is open: the alert stamp and the first
        # application move three hours back, the three quiet ones sit just
        # after the stamp.
        t0 = timezone.now() - timedelta(hours=3)
        RecruitmentApplication.objects.filter(id=first.id).update(applied_at=t0)
        for offset, application in enumerate(
            RecruitmentApplication.objects
            .filter(recruitment=self.recruitment)
            .exclude(id=first.id)
            .order_by("applied_at"),
            start=1,
        ):
            RecruitmentApplication.objects.filter(id=application.id).update(
                applied_at=t0 + timedelta(minutes=offset)
            )
        Recruitment.objects.filter(id=self.recruitment.id).update(
            last_applicant_alert_at=t0
        )

        mock_alert.reset_mock()
        latest = self._apply(self._player(5))

        self.assertEqual(mock_alert.call_count, 1)
        kwargs = mock_alert.call_args.kwargs
        # Three silent + this one. Not 5: the first one was already reported.
        self.assertEqual(kwargs["new_count"], 4)
        self.assertEqual(kwargs["total_count"], 5)
        self.assertEqual(kwargs["latest_application"], latest)

    def test_withdrawn_applications_count_towards_neither_total(
        self, _mock_received, mock_alert
    ):
        self._apply(self._player(1))
        second = self._apply(self._player(2))

        second.status = RecruitmentApplication.Status.WITHDRAWN
        second.save(update_fields=["status"])

        mock_alert.reset_mock()
        self._apply(self._player(3))

        kwargs = mock_alert.call_args.kwargs
        # Three rows exist; the withdrawn one is not somebody the org can
        # review, so it must not inflate the count or the tier.
        self.assertEqual(kwargs["total_count"], 2)

    def test_a_raising_email_service_does_not_fail_the_application(
        self, mock_received, mock_alert
    ):
        mock_received.side_effect = RuntimeError("template blew up")
        mock_alert.side_effect = RuntimeError("smtp is down")

        application = self._apply(self._player(1))

        application.refresh_from_db()
        self.assertEqual(
            application.status, RecruitmentApplication.Status.APPLIED
        )


class AlertRecipientTests(RecruitmentEmailFixture):
    """Who the org-side alert is addressed to."""

    def _add_member(self, index, role):
        user = User.objects.create_user(
            email=f"{role}{index}@example.com",
            password="pass1234",
            username=f"{role}{index}",
        )
        accept_current_terms(user)
        OrganizationMember.objects.create(
            organization=self.org, user=user, role=role
        )
        return user

    def test_only_owners_and_admins_are_addressed(self):
        admin = self._add_member(1, OrganizationMember.Role.ADMIN)
        self._add_member(2, OrganizationMember.Role.COACH)
        self._add_member(3, OrganizationMember.Role.STAFF)

        recipients = new_applicant_alert_recipients(self.org)

        self.assertCountEqual(recipients, [self.owner.email, admin.email])

    def test_inactive_and_email_less_members_are_dropped(self):
        gone = self._add_member(1, OrganizationMember.Role.ADMIN)
        gone.is_active = False
        gone.save(update_fields=["is_active"])

        # A phone-only account: the DB requires one of the two, and this is
        # exactly the member the alert must skip rather than hand "" to Resend.
        silent = self._add_member(2, OrganizationMember.Role.ADMIN)
        silent.email = None
        silent.phone = "+919000000002"
        silent.save(update_fields=["email", "phone"])

        self.assertEqual(
            new_applicant_alert_recipients(self.org), [self.owner.email]
        )

    def test_the_list_never_repeats_an_address(self):
        self._add_member(1, OrganizationMember.Role.ADMIN)

        recipients = new_applicant_alert_recipients(self.org)

        self.assertEqual(len(recipients), len(set(recipients)))

    def test_an_org_with_nobody_to_tell_sends_nothing(self):
        OrganizationMember.objects.filter(organization=self.org).delete()
        player = self._player(1)

        with patch(
            "utils.transactional_emails.send_email_async"
        ) as mock_send:
            self._apply(player)

        # The received-email to the player is a different send; assert on the
        # alert by checking no message went to an org address.
        self.assertEqual(new_applicant_alert_recipients(self.org), [])
        for call in mock_send.call_args_list:
            self.assertNotEqual(call.kwargs["to_email"], [])


class StatusChangeEmailTests(RecruitmentEmailFixture):
    """change_status mails the applicant — for the statuses that warrant it."""

    def setUp(self):
        super().setUp()
        self.player = self._player(1)
        with patch(
            "recruitments.services.application_service"
            ".send_new_applicant_alert_email"
        ), patch(
            "recruitments.services.application_service"
            ".send_application_received_email"
        ):
            self.application = self._apply(self.player)

        self.org_actor = Actor(
            actor_type="organization",
            organization=self.org,
            organization_member=OrganizationMember.objects.get(
                organization=self.org, user=self.owner
            ),
        )

    def _change(self, to_status):
        with self.captureOnCommitCallbacks(execute=True):
            return ApplicationService.change_status(
                self.org_actor,
                self.recruitment,
                [str(self.application.id)],
                to_status,
            )

    @patch("recruitments.services.application_service.send_application_status_email")
    def test_each_notifying_status_sends_exactly_one_email(self, mock_send):
        for to_status in ("shortlisted", "selected", "rejected"):
            with self.subTest(to_status=to_status):
                mock_send.reset_mock()

                self._change(to_status)

                self.assertEqual(mock_send.call_count, 1)
                kwargs = mock_send.call_args.kwargs
                self.assertEqual(kwargs["to_status"], to_status)
                self.assertEqual(kwargs["application"].id, self.application.id)

    @patch("utils.transactional_emails.send_email_async")
    def test_reviewing_produces_no_email(self, mock_send):
        self._change("reviewing")

        mock_send.assert_not_called()

    @patch("recruitments.services.application_service.send_application_status_email")
    def test_a_raising_email_service_does_not_fail_the_status_change(
        self, mock_send
    ):
        mock_send.side_effect = RuntimeError("template blew up")

        result = self._change("shortlisted")

        self.assertEqual(result["updated"], [str(self.application.id)])
        self.application.refresh_from_db()
        self.assertEqual(self.application.status, "shortlisted")


class RecruitmentEmailRenderingTests(RecruitmentEmailFixture):
    """The finished mail, for the emails whose copy varies by status."""

    def setUp(self):
        super().setUp()
        self.player = self._player(1)
        with patch(
            "recruitments.services.application_service"
            ".send_new_applicant_alert_email"
        ), patch(
            "recruitments.services.application_service"
            ".send_application_received_email"
        ):
            self.application = self._apply(self.player)

        self.application.applied_position = self.position
        self.application.save(update_fields=["applied_position"])

    def _sent(self, to_status):
        with patch("utils.transactional_emails.send_email_async") as mock_send:
            send_application_status_email(
                application=self.application, to_status=to_status
            )

        self.assertEqual(mock_send.call_count, 1)
        return mock_send.call_args.kwargs

    def test_every_notifying_status_renders_its_own_badge_and_subject(self):
        expected = {
            "selected": ("Selected", "You're selected"),
            "shortlisted": ("Shortlisted", "You've been shortlisted"),
            "invited": ("Invited", "You're invited"),
            "rejected": ("Not selected", "Update on your application"),
        }

        for to_status, (badge, subject_start) in expected.items():
            with self.subTest(to_status=to_status):
                kwargs = self._sent(to_status)
                html = kwargs["html_message"]

                self.assertTrue(kwargs["subject"].startswith(subject_start))
                self.assertIn(self.recruitment.title, kwargs["subject"])
                self.assertIn(f">{badge}</span>", html)
                self.assertIn(self.org.name, html)
                self.assertIn("Striker", html)
                self.assertIn("Thiruvananthapuram, Kerala", html)
                self.assertNotIn("{{", html)
                self.assertNotIn("{%", html)

    def test_rejected_points_at_the_recruitment_list_not_the_recruitment(self):
        base = "https://goatza.com"
        with override_settings(FRONTEND_BASE_URL=base):
            rejected = self._sent("rejected")["html_message"]
            selected = self._sent("selected")["html_message"]

        self.assertIn(f'href="{base}/recruitments"', rejected)
        self.assertIn("Explore recruitments", rejected)
        self.assertIn(
            f'href="{base}/recruitments/{self.recruitment.id}"', selected
        )
        self.assertIn("View details", selected)

    def test_a_status_nobody_should_be_mailed_about_sends_nothing(self):
        for to_status in ("reviewing", "withdrawn", "applied"):
            with self.subTest(to_status=to_status):
                with patch(
                    "utils.transactional_emails.send_email_async"
                ) as mock_send:
                    send_application_status_email(
                        application=self.application, to_status=to_status
                    )

                mock_send.assert_not_called()

    def test_an_applicant_with_no_email_is_skipped(self):
        self.player.email = None
        self.player.phone = "+919000000001"
        self.player.save(update_fields=["email", "phone"])
        self.application.refresh_from_db()

        with patch("utils.transactional_emails.send_email_async") as mock_send:
            send_application_status_email(
                application=self.application, to_status="selected"
            )

        mock_send.assert_not_called()

    def test_a_missing_position_drops_its_segment_rather_than_the_separator(self):
        self.application.applied_position = None
        self.application.save(update_fields=["applied_position"])

        html = self._sent("selected")["html_message"]

        self.assertIn(
            f"{self.org.name}</b> &middot; Thiruvananthapuram, Kerala", html
        )
        self.assertNotIn("&middot;  &middot;", html)
