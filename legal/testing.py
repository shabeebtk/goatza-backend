"""
Test helper: give a user the consent a real one would already have.

WHY EVERY TEST FIXTURE NEEDS THIS

`User.objects.create_user` builds an account that production cannot produce.
Every real path to a user record — the signup view and the Google callback —
records acceptance in the same transaction that creates the row, so a saved
user with `terms_version = NULL` is a state the application never reaches.

A fixture that skips it is therefore not "a plain user", it is a user sitting
behind the consent gate, and every write it attempts answers 403. That is the
gate working, not the test failing.

The one exception is `legal/tests/`, which is about the gate itself and builds
pending users deliberately.

Not named `test_*` so the test runner does not try to collect it as a module of
tests.
"""

from legal.constants import REQUIRED_DOCUMENTS
from legal.services.acceptance_service import record_acceptance


def accept_current_terms(user):
    """
    Record acceptance of every gating document at the current version, exactly
    as signup does. Returns the user, so it can wrap a factory's return.
    """
    record_acceptance(user=user, documents=list(REQUIRED_DOCUMENTS))
    return user
