"""
The date of birth: set at signup and onboarding, then locked.

The lock is what stops it being a preference ("refused at 12, come back as 20").
The minor rule on every accepted change is what stops a change getting round the
parental gate. The admin route is what keeps the legal right to correct wrong
personal data.
"""

from datetime import date, timedelta
from unittest.mock import Mock, patch

from django.contrib.admin.sites import AdminSite
from django.contrib.messages import get_messages
from django.contrib.messages.storage.fallback import FallbackStorage
from django.core.cache import cache
from django.test import RequestFactory, TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.admin import (
    WITHDRAWN_NEEDS_REVIEW_MESSAGE,
    UserAdmin,
    UserProfileAdmin,
)
from apps.accounts.models import User, UserProfile
from apps.accounts.services.age_service import birthdate_editable
from apps.guardians.management.commands.purge_unconsented import stale_pending_users
from apps.guardians.models import GuardianConsentEvent
from apps.guardians.services.consent_service import (
    clear_pending_for_adult,
    request_consent,
)
from apps.legal.testing import accept_current_terms

PROFILE_URL = "/user/update/profile/data"
DETAILS_URL = "/user/details"

LOCKED_SENTENCE = (
    "Your date of birth can't be changed. If it's wrong, contact support."
)

NOT_NEEDED = User.GuardianConsentStatus.NOT_NEEDED
PENDING = User.GuardianConsentStatus.PENDING
APPROVED = User.GuardianConsentStatus.APPROVED
WITHDRAWN = User.GuardianConsentStatus.WITHDRAWN


def years_ago(years):
    today = timezone.now().date()
    return date(today.year - years, 6, 15)


class BirthdateTestBase(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()

    def _user(self, *, onboarded, birthdate=None, username="dob", **fields):
        user = User.objects.create_user(
            email=f"{username}@example.com",
            username=username,
            password="password123",
            country_code="IN",
            is_onboarding_completed=onboarded,
            **fields,
        )
        accept_current_terms(user)
        UserProfile.objects.create(user=user, name="Dee", birthdate=birthdate)
        return User.objects.select_related("profile").get(pk=user.pk)

    def _patch(self, user, **body):
        self.client.force_authenticate(user=user)
        return self.client.patch(PROFILE_URL, body, format="json")

    def _set(self, user, value):
        return self._patch(user, birthdate=value.isoformat())

    def _stored(self, user):
        return UserProfile.objects.get(user=user).birthdate

    def _status(self, user):
        return User.objects.get(pk=user.pk).guardian_consent_status


class BirthdateEditableTests(BirthdateTestBase):
    def test_until_onboarding_is_done(self):
        user = self._user(onboarded=False, birthdate=years_ago(20))

        self.assertTrue(birthdate_editable(user))

    def test_once_more_for_a_legacy_account_with_none(self):
        user = self._user(onboarded=True)

        self.assertTrue(birthdate_editable(user))

    def test_not_after_that(self):
        user = self._user(onboarded=True, birthdate=years_ago(20))

        self.assertFalse(birthdate_editable(user))


class ProfileUpdateTests(BirthdateTestBase):
    def test_during_onboarding_it_can_change_more_than_once(self):
        user = self._user(onboarded=False, birthdate=years_ago(20))

        for years in (21, 22, 23):
            response = self._set(user, years_ago(years))

            self.assertEqual(response.status_code, 200, response.data)
            self.assertEqual(self._stored(user), years_ago(years))

    def test_after_onboarding_it_is_refused_with_the_sentence(self):
        user = self._user(onboarded=True, birthdate=years_ago(20))

        response = self._set(user, years_ago(30))

        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(response.data["data"]["birthdate"], [LOCKED_SENTENCE])
        self.assertEqual(self._stored(user), years_ago(20))

    def test_a_legacy_account_sets_it_once_and_then_it_is_locked(self):
        user = self._user(onboarded=True)

        first = self._set(user, years_ago(30))
        self.assertEqual(first.status_code, 200, first.data)
        self.assertFalse(first.data["data"]["can_edit_birthdate"])

        user = User.objects.select_related("profile").get(pk=user.pk)
        second = self._set(user, years_ago(31))

        self.assertEqual(second.status_code, 400, second.data)
        self.assertEqual(self._stored(user), years_ago(30))

    def test_it_can_never_be_cleared(self):
        for onboarded in (False, True):
            with self.subTest(onboarded=onboarded):
                user = self._user(
                    onboarded=onboarded,
                    birthdate=years_ago(20),
                    username=f"dob{onboarded}",
                )

                response = self._patch(user, birthdate=None)

                self.assertEqual(response.status_code, 400, response.data)
                self.assertIn("birthdate", response.data["data"])
                self.assertEqual(self._stored(user), years_ago(20))

    def test_it_cannot_be_in_the_future(self):
        user = self._user(onboarded=False, birthdate=years_ago(20))

        response = self._patch(user, birthdate="2999-01-01")

        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(self._stored(user), years_ago(20))

    def test_the_same_date_is_a_no_op_even_when_locked(self):
        user = self._user(onboarded=True, birthdate=years_ago(20))

        response = self._patch(
            user, headline="Striker", birthdate=years_ago(20).isoformat()
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["headline"], "Striker")
        self.assertIs(response.data["data"]["guardian_required"], False)

    def test_the_old_counter_is_not_touched(self):
        user = self._user(onboarded=False, birthdate=years_ago(20))

        self._set(user, years_ago(21))

        self.assertEqual(User.objects.get(pk=user.pk).birthdate_corrections, 0)

    def test_the_jurisdiction_cannot_be_changed_here(self):
        # A user who could flip IN to GB would have the age gate's own hole,
        # reopened on a screen with no age check on it.
        user = self._user(onboarded=False, birthdate=years_ago(20))

        self._patch(user, country_code="GB")

        self.assertEqual(User.objects.get(pk=user.pk).country_code, "IN")


class MinorRuleTests(BirthdateTestBase):
    def test_a_date_that_makes_a_minor_locks_them_and_says_so_at_the_top_level(self):
        user = self._user(onboarded=False, birthdate=years_ago(20))

        response = self._set(user, years_ago(15))

        self.assertEqual(response.status_code, 200, response.data)
        self.assertIs(response.data["data"]["guardian_required"], True)
        self.assertEqual(self._status(user), PENDING)

    def test_a_legacy_first_set_runs_the_same_rule(self):
        user = self._user(onboarded=True)

        response = self._set(user, years_ago(15))

        self.assertIs(response.data["data"]["guardian_required"], True)
        self.assertEqual(self._status(user), PENDING)

    def test_an_adult_date_needs_nothing(self):
        user = self._user(onboarded=False, birthdate=years_ago(20))

        response = self._set(user, years_ago(30))

        self.assertIs(response.data["data"]["guardian_required"], False)
        self.assertEqual(self._status(user), NOT_NEEDED)

    def test_a_pending_minor_cannot_reach_the_endpoint(self):
        user = self._user(
            onboarded=False,
            birthdate=years_ago(15),
            guardian_consent_status=PENDING,
        )

        response = self._set(user, years_ago(30))

        self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(self._stored(user), years_ago(15))
        self.assertEqual(self._status(user), PENDING)


class DetailsTests(BirthdateTestBase):
    def _details(self, user):
        self.client.force_authenticate(user=user)
        return self.client.get(DETAILS_URL, {"list_type": "full"})

    def test_it_is_on_both_the_full_and_the_mini_payload(self):
        user = self._user(onboarded=False, birthdate=years_ago(20))

        self.assertIs(self._details(user).data["data"]["can_edit_birthdate"], True)

        self.client.force_authenticate(user=user)
        mini = self.client.get(DETAILS_URL)
        self.assertIs(mini.data["data"]["can_edit_birthdate"], True)

    def test_it_follows_the_lock(self):
        locked = self._user(onboarded=True, birthdate=years_ago(20), username="locked")
        legacy = self._user(onboarded=True, username="legacy")

        self.assertIs(self._details(locked).data["data"]["can_edit_birthdate"], False)
        self.assertIs(self._details(legacy).data["data"]["can_edit_birthdate"], True)


class SupportRouteTests(BirthdateTestBase):
    """Staff correct it in the Django admin, under the same rule."""

    def setUp(self):
        super().setUp()
        self.staff = User.objects.create_user(
            email="staff@example.com",
            username="staffer",
            password="password123",
            is_staff=True,
        )
        self.request = RequestFactory().post("/admin/")
        self.request.user = self.staff
        # What MessageMiddleware does on a real admin request.
        self.request.session = {}
        self.request._messages = FallbackStorage(self.request)
        self.admin = UserProfileAdmin(UserProfile, AdminSite())

    def _warnings(self):
        return [str(message) for message in get_messages(self.request)]

    def _admin_change(self, user, value):
        profile = UserProfile.objects.get(user=user)
        profile.birthdate = value
        self.admin.save_model(
            self.request, profile, Mock(changed_data=["birthdate"]), change=True
        )

    def test_a_correction_to_a_minors_date_locks_them(self):
        user = self._user(onboarded=True, birthdate=years_ago(20))

        self._admin_change(user, years_ago(15))

        self.assertEqual(self._stored(user), years_ago(15))
        self.assertEqual(self._status(user), PENDING)

    def test_a_pending_minor_corrected_to_an_adult_is_cleared(self):
        user = self._user(
            onboarded=True,
            birthdate=years_ago(15),
            guardian_consent_status=PENDING,
        )

        self._admin_change(user, years_ago(30))

        self.assertEqual(self._status(user), NOT_NEEDED)
        self.assertEqual(self._warnings(), [])

    def test_once_cleared_the_purge_no_longer_picks_them(self):
        user = self._user(onboarded=True, birthdate=years_ago(15))
        user.guardian_consent_status = PENDING
        user.save(update_fields=["guardian_consent_status"])
        with patch("apps.guardians.services.consent_service.send_guardian_consent_request_email"):
            request_consent(
                child=user, parent_name="Priya Nair", parent_contact="parent@example.com"
            )
        GuardianConsentEvent.objects.filter(child=user).update(
            created_at=timezone.now() - timedelta(days=60)
        )
        cutoff = timezone.now() - timedelta(days=30)
        self.assertIn(user, stale_pending_users(cutoff))

        self._admin_change(user, years_ago(30))

        self.assertNotIn(user, stale_pending_users(cutoff))

    def test_approved_stays_approved(self):
        user = self._user(
            onboarded=True,
            birthdate=years_ago(15),
            guardian_consent_status=APPROVED,
        )

        self._admin_change(user, years_ago(30))

        self.assertEqual(self._status(user), APPROVED)
        self.assertEqual(self._warnings(), [])

    def test_withdrawn_stays_withdrawn_and_staff_are_told(self):
        user = self._user(
            onboarded=True,
            birthdate=years_ago(15),
            guardian_consent_status=WITHDRAWN,
        )

        self._admin_change(user, years_ago(30))

        self.assertEqual(self._status(user), WITHDRAWN)
        self.assertEqual(self._warnings(), [WITHDRAWN_NEEDS_REVIEW_MESSAGE])

    def test_withdrawn_with_a_minors_date_says_nothing_new(self):
        # Still a child, and still locked: that is simply the rule working.
        user = self._user(
            onboarded=True,
            birthdate=years_ago(15),
            guardian_consent_status=WITHDRAWN,
        )

        self._admin_change(user, years_ago(16))

        self.assertEqual(self._status(user), WITHDRAWN)
        self.assertEqual(self._warnings(), [])

    def test_a_minor_corrected_to_another_minors_date_stays_pending(self):
        user = self._user(
            onboarded=True,
            birthdate=years_ago(15),
            guardian_consent_status=PENDING,
        )

        self._admin_change(user, years_ago(16))

        self.assertEqual(self._status(user), PENDING)

    def test_the_clear_itself_leaves_a_minor_alone(self):
        user = self._user(
            onboarded=True,
            birthdate=years_ago(15),
            guardian_consent_status=PENDING,
        )

        self.assertFalse(clear_pending_for_adult(user, staff_id=self.staff.pk))
        self.assertEqual(self._status(user), PENDING)

    def test_the_log_has_old_and_new_status_and_never_the_date(self):
        user = self._user(
            onboarded=True,
            birthdate=years_ago(15),
            guardian_consent_status=PENDING,
        )

        with self.assertLogs("apps.guardians.services.consent_service", "INFO") as service:
            with self.assertLogs("apps.accounts.admin", "INFO") as admin_log:
                self._admin_change(user, years_ago(30))

        for line in (service.output[0], admin_log.output[0]):
            self.assertIn(str(user.pk), line)
            self.assertIn(str(self.staff.pk), line)
            self.assertIn("not_needed", line)
            self.assertIn("pending", line)
            self.assertNotIn(str(years_ago(30)), line)
            self.assertNotIn(str(years_ago(30).year), line)

    def test_it_records_who_did_it_and_not_the_date(self):
        user = self._user(onboarded=True, birthdate=years_ago(20))

        with self.assertLogs("apps.accounts.admin", "INFO") as logs:
            self._admin_change(user, years_ago(30))

        line = logs.output[0]
        self.assertIn(str(self.staff.pk), line)
        self.assertIn(str(user.pk), line)
        self.assertNotIn(str(years_ago(30)), line)

    def test_saving_something_else_runs_nothing(self):
        user = self._user(onboarded=True, birthdate=years_ago(15))
        profile = UserProfile.objects.get(user=user)

        self.admin.save_model(
            self.request, profile, Mock(changed_data=["headline"]), change=True
        )

        self.assertEqual(self._status(user), NOT_NEEDED)

    def test_clearing_it_does_not_lock_anybody(self):
        # Unknown reads as a minor, and a legacy account with no date was
        # deliberately never locked for that.
        user = self._user(onboarded=True, birthdate=years_ago(20))

        self._admin_change(user, None)

        self.assertEqual(self._status(user), NOT_NEEDED)

    def test_the_inline_on_the_user_page_runs_it_too(self):
        user = self._user(onboarded=True, birthdate=years_ago(20))
        profile = UserProfile.objects.get(user=user)
        profile.birthdate = years_ago(15)
        profile.save(update_fields=["birthdate"])

        formset = Mock(
            model=UserProfile,
            forms=[Mock(changed_data=["birthdate"], instance=profile)],
        )

        UserAdmin(User, AdminSite()).save_formset(
            self.request, Mock(), formset, change=True
        )

        self.assertEqual(self._status(user), PENDING)
