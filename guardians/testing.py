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

from guardians.services.consent_service import (
    record_shared_contact_approval,
    request_consent,
)

DEFAULT_PARENT_NAME = "Test Guardian"


def grant_guardian_consent(user, parent_name=DEFAULT_PARENT_NAME):
    """
    Put an approved consent on file for ``user`` and unlock the account.
    Returns the user, so it can wrap a factory's return.

    GOES THROUGH THE REAL SERVICE rather than writing the rows itself, for the
    same reason ``legal.testing`` calls ``record_acceptance``: a helper that
    INSERTs its own event is a helper that keeps passing after the service's
    invariants change, and a fixture that quietly disagrees with production is
    worse than no fixture.

    It uses the SHARED-CONTACT route — the guardian's contact is the user's
    own — which is deliberate and not just convenient. That path sends no
    email and mints no token, so a test that only wants an unlocked account
    does not have to stub a mailer or carry a token around. The row it leaves
    behind is honest about what it is: ``method=shared_contact``, the weakest
    of the three, which is exactly what a fixture deserves to be recorded as.

    Harmless to call twice — the approval is idempotent.
    """
    contact = user.email or user.phone

    if not contact:
        raise ValueError(
            "grant_guardian_consent needs a user with an email or a phone — "
            "the guardian is reached at the user's own contact"
        )

    request_consent(
        child=user, parent_name=parent_name, parent_contact=contact
    )
    record_shared_contact_approval(child=user, parent_name=parent_name)

    user.refresh_from_db()
    return user
