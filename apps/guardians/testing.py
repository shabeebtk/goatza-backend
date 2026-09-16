"""
Test helper: give a minor the guardian consent a real one would already have.

WHY A FIXTURE NEEDS THIS

``User.objects.create_user`` plus a profile with a birthdate builds an account
that production cannot produce. Every real path to a minor's account — OTP
verification on the email signup, the role step on the Google one — runs
``ensure_pending_for_minor`` in the same breath, so a saved minor whose
``guardian_consent_status`` is still ``not_needed`` is a state the application
never reaches.

The direction that bites is the other one, though. A fixture that gives a user
a birthdate 14 years ago has not built "a plain user", it has built a LOCKED
one: every request it makes answers 403 GUARDIAN_CONSENT_REQUIRED, including
the reads, because this gate blocks those too (see guardians/permissions.py).
That is the gate working, not the test failing — and this function is the fix,
exactly as ``legal.testing.accept_current_terms`` is the fix for the other gate.

Tests that are ABOUT the lock build pending minors deliberately; everything
else calls this and forgets the whole subject exists.

Not named ``test_*`` so the runner does not try to collect it as a module of
tests.
"""

from unittest.mock import patch

from apps.accounts.models import User
from apps.guardians.services import consent_service
from apps.guardians.services.consent_service import approve_by_token, request_consent

DEFAULT_PARENT_NAME = "Test Guardian"


def grant_guardian_consent(user, parent_name=DEFAULT_PARENT_NAME):
    """
    Put an approved consent on file for ``user`` and unlock the account.
    Returns the user, so it can wrap a factory's return.

    GOES THROUGH THE REAL SERVICE rather than writing the rows itself, for the
    same reason ``legal.testing`` calls ``record_acceptance``: a helper that
    INSERTs its own event is a helper that keeps passing after the service's
    invariants change, and a fixture that quietly disagrees with production is
    worse than no fixture. So it is the real exchange — ``request_consent``
    mints a link, ``approve_by_token`` answers it.

    THE GUARDIAN IS THE USER'S OWN CONTACT, deliberately. The row it leaves
    behind is then honest about what it is: ``method=shared_contact``, the
    weakest of the three, which is exactly what a fixture deserves to be
    recorded as. It also means the helper needs nothing a test would have to
    invent — no second address, no second account.

    NO EMAIL LEAVES, AND NO TOKEN HAS TO BE FISHED OUT OF ONE. The raw token
    exists only at mint time (the service stores a hash and drops it), so
    ``_mint_token`` is patched to hand back one generated here, and the mailer
    is patched to a mock for the duration — the send is deferred to
    ``on_commit``, which inside a ``TestCase`` never fires and inside a
    ``TransactionTestCase`` fires within the patch. Works for an email-only
    and a phone-only user alike: a phone contact records the request and
    delivers nothing, and the link approves the same way.

    Harmless to call twice — an account that is already approved is returned
    untouched rather than asked again.
    """
    user.refresh_from_db()

    if user.guardian_consent_status == User.GuardianConsentStatus.APPROVED:
        return user

    contact = user.email or user.phone

    if not contact:
        raise ValueError(
            "grant_guardian_consent needs a user with an email or a phone — "
            "the guardian is reached at the user's own contact"
        )

    raw_token, hashed, expires_at = consent_service._mint_token()

    with (
        patch.object(
            consent_service, "_mint_token", return_value=(raw_token, hashed, expires_at)
        ),
        patch.object(consent_service, "send_guardian_consent_request_email"),
    ):
        request_consent(
            child=user, parent_name=parent_name, parent_contact=contact
        )

    approve_by_token(raw_token=raw_token, parent_name=parent_name)

    user.refresh_from_db()
    return user
