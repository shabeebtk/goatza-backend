"""
Shared fixtures for the guardian tests.

Two things here are worth reading before writing a test in this package.

THE RAW TOKEN ONLY EXISTS INSIDE AN EMAIL. It is generated, hashed, the hash is
stored and the plaintext is dropped (consent_service), so a test cannot read one
out of the database — by design, since a database that yielded working consent
links would be the exact failure the hashing prevents. ``consent_link`` is how a
test gets one: it patches the sender and reads the URL that was about to go out.

THE SEND HAPPENS ON COMMIT. ``request_consent`` defers the email with
``transaction.on_commit`` so a rolled-back request cannot mail a live link, and
inside a ``TestCase`` nothing ever commits — the callback would simply never
run and the mock would never be called. ``captureOnCommitCallbacks(execute=True)``
is what makes the deferred send happen inside the test. Forget it and the
symptom is a mock with zero calls and a token you cannot find.
"""

import datetime
from contextlib import contextmanager
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import User, UserProfile
from guardians.services.consent_service import ensure_pending_for_minor
from legal.testing import accept_current_terms
from usernames.services.username_service import UsernameService

MINOR_YEARS = 14
ADULT_YEARS = 30

PARENT_EMAIL = "parent@example.com"
PARENT_NAME = "Priya Nair"
PASSWORD = "password123"

# Endpoints under test, named once. The child's side is authenticated; the
# parent's is anonymous and addressed by token.
DETAILS_URL = "/user/details"
FEED_URL = "/feed/list"
GUARDIAN_DETAILS_URL = "/guardian/details"
GUARDIAN_RESEND_URL = "/guardian/resend"
GUARDIAN_SHARED_APPROVE_URL = "/guardian/shared/approve"


def consent_url(token):
    return f"/guardian/consent/{token}"


def years_ago(years):
    return datetime.date.today() - datetime.timedelta(days=365 * years)


def token_from(url):
    """The raw token out of a consent link."""
    return url.split("token=")[1]


def make_user(email, username, age_years, phone=None):
    """
    An account built the way signup builds one: profile, birthdate, terms on
    file, and ``ensure_pending_for_minor`` run — which locks a minor and leaves
    an adult alone.
    """
    user = User.objects.create_user(
        email=email, password=PASSWORD, phone=phone, country_code="IN"
    )
    UserProfile.objects.create(
        user=user, name="Test Player", birthdate=years_ago(age_years)
    )
    UsernameService.claim(username, user=user)
    accept_current_terms(user)
    ensure_pending_for_minor(user)
    user.refresh_from_db()
    return user


def make_minor(email="minor@example.com", username="testminor", phone=None):
    return make_user(email, username, MINOR_YEARS, phone=phone)


def make_adult(email="adult@example.com", username="testadult", phone=None):
    return make_user(email, username, ADULT_YEARS, phone=phone)


class GuardianTestCase(TestCase):
    """Cache-clean, actor-headed API client. See the module docstring."""

    def setUp(self):
        # Every guardian endpoint is throttled, and the counters live in the
        # shared cache — without this a test inherits whatever budget the ones
        # before it spent and starts failing with 429 for no visible reason.
        cache.clear()
        self.client = APIClient()

    def authenticate(self, user):
        self.client.force_authenticate(user=user)
        # Every BaseAPIView resolves an actor before the view body runs, so a
        # missing header would refuse the request before the thing under test
        # got a chance to.
        self.client.credentials(
            HTTP_X_ACTOR_TYPE="user", HTTP_X_ACTOR_ID=str(user.id)
        )
        return user

    @contextmanager
    def sending_consent_email(self):
        """
        Run a block with the consent mailer patched and on-commit callbacks
        executed. Yields the mock, so a test can assert what was sent — or that
        nothing was.
        """
        with patch(
            "guardians.services.consent_service"
            ".send_guardian_consent_request_email"
        ) as sender:
            with self.captureOnCommitCallbacks(execute=True):
                yield sender

    def ask_for_consent(self, parent_email=PARENT_EMAIL, parent_name=PARENT_NAME):
        """
        POST /guardian/details as the authenticated child, and return
        ``(response, raw_token_or_None)``.

        The token is None for a shared contact, which sends nothing — that is
        the assertion several tests are actually making.
        """
        with self.sending_consent_email() as sender:
            response = self.client.post(
                GUARDIAN_DETAILS_URL,
                {"parent_name": parent_name, "parent_email": parent_email},
                format="json",
            )

        # READ AFTER THE BLOCK, never inside it. The send is deferred to
        # on_commit, and captureOnCommitCallbacks runs the callbacks as it
        # EXITS — a read one line earlier sees a mock that was never called.
        if not sender.call_args_list:
            return response, None

        return response, token_from(sender.call_args.kwargs["consent_url"])

    def resend_consent(self):
        """POST /guardian/resend, returning ``(response, fresh_token)``."""
        with self.sending_consent_email() as sender:
            response = self.client.post(GUARDIAN_RESEND_URL, {}, format="json")

        if not sender.call_args_list:
            return response, None

        return response, token_from(sender.call_args.kwargs["consent_url"])

    def approve_by_link(self, token, parent_name=PARENT_NAME):
        return self.parent_post(f"{consent_url(token)}/approve", {
            "parent_name": parent_name,
            "confirm_18_plus": True,
        })

    def parent_post(self, url, body=None):
        """
        A POST from the parent's side: no session, no actor headers, nothing
        but the token in the URL. A fresh client, because ``self.client`` is
        holding the child's credentials.
        """
        client = APIClient()

        with self.captureOnCommitCallbacks(execute=True):
            return client.post(url, body or {}, format="json")

    def parent_get(self, url):
        return APIClient().get(url)

    def assert_status(self, user, expected):
        user.refresh_from_db()
        self.assertEqual(user.guardian_consent_status, expected)
