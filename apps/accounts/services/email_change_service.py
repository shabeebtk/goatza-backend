"""
Changing the login email — the highest-stakes write a user can make.

WHY THIS IS NOT AN ORDINARY PROFILE FIELD

``email`` is ``USERNAME_FIELD``. It is the identifier the login view
authenticates against AND the inbox every forgot-password code is mailed to.
Whoever controls the address on an account controls the account: they can sign
in, and if they cannot they can mail themselves a reset. So changing it is not
"editing a contact detail", it is handing the account over — and the only safe
version of that is one where both halves are proved.

TWO PROOFS, IN THIS ORDER

  * ``initiate_email_change`` asks for the CURRENT PASSWORD. That is the
    caller proving they are the account's owner and not somebody sitting at an
    unlocked laptop or holding a stolen access token.
  * ``confirm_email_change`` spends a one-time code mailed to the NEW address.
    That is the caller proving the destination inbox is theirs, before a single
    byte is written — an account must never be moved to an address that turns
    out to be a typo, because the recovery path goes to that same typo.

Nothing is written until both have passed. Between the two steps the pending
address lives in a SERVER-SIDE binding (``CacheKeys.email_change_pending``), so
confirm never trusts a client-supplied address; see that key for why.

The whole flow is gated on the account having a usable password — see
``PASSWORD_NOT_SET_MESSAGE`` for the Google-sign-in reasoning, which is the
subtlest thing in this module.
"""

import logging

from django.db import IntegrityError, transaction
from rest_framework.exceptions import ValidationError

from apps.accounts.models import User
# mask_email lives with the deletion flow because that is where it was first
# needed; it is the same masking rule and there is no second version of it.
from apps.accounts.services.account_deletion_service import mask_email
from utils.cache import cache_delete, cache_get, cache_set
from utils.cache_keys import CacheKeys
from utils.otp_validation import OTP_EXPIRE_MINUTES, generate_otp, verify_otp
from utils.transactional_emails import (
    send_email_change_otp_email,
    send_email_changed_notice,
)
from utils.validations import is_valid_email

logger = logging.getLogger(__name__)

# Scopes the code to this flow and to the NEW address, exactly as
# DELETION_OTP_PURPOSE does for deletion. Without it, a forgot-password code —
# which anyone who reaches an inbox can request — would also confirm an email
# change, and the second proof would be worth nothing.
EMAIL_CHANGE_OTP_PURPOSE = "email_change"

# The pending binding dies with the code it belongs to. One TTL, so there is no
# window in which a code is still good but the address it was issued for has
# been forgotten (or, worse, the other way round).
PENDING_TTL_SECONDS = OTP_EXPIRE_MINUTES * 60

# Said to a caller whose code did not match, whose code expired, and whose
# pending change does not exist at all. ONE message for all three, on the same
# reasoning as INVALID_CREDENTIAL_MESSAGE next door: an attacker holding an
# access token must not be able to tell "wrong digits" from "nothing pending"
# — the second one is a free confirmation that somebody else's change is in
# flight.
INVALID_CODE_MESSAGE = "The code you entered is incorrect or has expired."

# Google sign-in matches users BY EMAIL — ``User.objects.get_or_create(
# email=...)`` in user_google_auth_views.py. So the moment a Google-only
# account's email moved, that button would stop finding this account and start
# creating (or logging into) a DIFFERENT one, and the original would be
# unreachable by the only credential its owner has ever used.
#
# The password is the continuity credential: an account that has one can still
# be signed into at the new address the instant the change lands. So the flow
# refuses accounts that do not have one, and points at the flow that sets one.
PASSWORD_NOT_SET_MESSAGE = (
    "Your account doesn't have a password yet, so you signed in with Google. "
    "Set a password first using \"Forgot password\", then come back and change "
    "your email."
)

# Verbatim from the signup view's duplicate-email answer. The same collision
# gets the same sentence wherever a user meets it.
EMAIL_TAKEN_MESSAGE = "User already exists"

INVALID_PASSWORD_MESSAGE = "Your password is incorrect."


class EmailChangeError(ValidationError):
    """
    A ValidationError that also carries a stable machine code.

    The client has to tell these apart — "set a password first" is the one
    refusal that needs a link to somewhere else, and a wrong password belongs
    on the password field rather than in a banner — and matching on English
    sentences is how error copy becomes unchangeable. The code is the contract;
    the message is free to be reworded.
    """

    def __init__(self, message, code):
        super().__init__(message)
        self.error_code = code


# ─────────────────────────────────────────────
# STEP 1 — INITIATE
# ─────────────────────────────────────────────

def _assert_can_change_email(user, password):
    """The owner check. Runs before anything is validated, cached or mailed."""
    if not user.has_usable_password():
        # A Google-only account holds a random hash nobody has ever been shown,
        # so check_password below could only ever fail — say the useful thing
        # instead of "your password is incorrect".
        raise EmailChangeError(PASSWORD_NOT_SET_MESSAGE, "password_not_set")

    if not password or not user.check_password(password):
        raise EmailChangeError(INVALID_PASSWORD_MESSAGE, "invalid_password")


def _clean_new_email(user, new_email):
    """Normalize and vet the requested address, returning what will be stored."""
    new_email = (new_email or "").strip().lower()

    if not new_email or not is_valid_email(new_email):
        raise EmailChangeError("Enter a valid email address.", "invalid_email")

    # Compared lowercased on both sides: addresses that differ only in case are
    # the same mailbox, and letting one through would mail a code to the
    # address the user is already signed in with.
    if new_email == (user.email or "").strip().lower():
        raise EmailChangeError(
            "This is already your email address.", "same_email"
        )

    if User.objects.filter(email__iexact=new_email).exclude(pk=user.pk).exists():
        raise EmailChangeError(EMAIL_TAKEN_MESSAGE, "email_taken")

    return new_email


def initiate_email_change(user, *, new_email, password):
    """
    Prove ownership of the ACCOUNT, then mail a code to the NEW address.

    Returns ``{"sent_to": "<masked>", "expires_in": <seconds>}`` — masked
    because this response is rendered on a screen someone else may be looking
    at, and the caller already knows what they typed.

    Writes NOTHING to the user row. The only state this leaves behind is the
    pending binding, which expires on its own.
    """
    _assert_can_change_email(user, password)

    new_email = _clean_new_email(user, new_email)

    # Keyed to the NEW address, not the account's: the code has to be spendable
    # only by whoever can read the inbox it was sent to.
    otp = generate_otp(new_email, purpose=EMAIL_CHANGE_OTP_PURPOSE)

    # Written BEFORE the mail goes out. The send is fire-and-forget on a daemon
    # thread, so a binding written afterwards could lose a race with a very
    # fast confirm; a binding with no code behind it just expires.
    cache_set(
        CacheKeys.email_change_pending(user.id),
        new_email,
        timeout=PENDING_TTL_SECONDS,
    )

    send_email_change_otp_email(
        name=user.profile_name, email=new_email, otp=otp
    )

    logger.info(f"Email change OTP sent | user={user.id}")

    return {
        "sent_to": mask_email(new_email),
        "expires_in": PENDING_TTL_SECONDS,
    }


# ─────────────────────────────────────────────
# STEP 2 — CONFIRM
# ─────────────────────────────────────────────

def confirm_email_change(user, *, otp):
    """
    Spend the code and move the account to the address initiate bound.

    Returns the new email so the caller can hand it back to the client, whose
    local copy of the user is now stale.

    DELIBERATELY DOES NOT BLACKLIST REFRESH TOKENS, unlike the password change
    and the deletion next door: the owner has just proved themselves twice in
    two minutes, so signing every one of their devices out would be punishing
    the person who did the right thing. Revoke here if the day comes when this
    step can be reached with only one proof.
    """
    new_email = cache_get(CacheKeys.email_change_pending(user.id))

    # Nothing pending, or it expired. Same answer as a wrong code — see
    # INVALID_CODE_MESSAGE.
    if not new_email:
        raise EmailChangeError(INVALID_CODE_MESSAGE, "invalid_code")

    # verify_otp consumes the code on success, so it cannot be replayed.
    if not otp or not verify_otp(
        new_email, str(otp), purpose=EMAIL_CHANGE_OTP_PURPOSE
    ):
        raise EmailChangeError(INVALID_CODE_MESSAGE, "invalid_code")

    old_email = user.email

    try:
        with transaction.atomic():
            # Re-checked HERE, not just in initiate: ten minutes can pass
            # between the two steps, and somebody else can sign up with this
            # address in them. The IntegrityError below is the same check for
            # the race the query cannot close.
            if (
                User.objects
                .filter(email__iexact=new_email)
                .exclude(pk=user.pk)
                .exists()
            ):
                raise EmailChangeError(EMAIL_TAKEN_MESSAGE, "email_taken")

            user.email = new_email
            # The code that just landed IS the verification — the address
            # proved itself the same way a signup one does.
            user.is_email_verified = True
            user.save(
                update_fields=["email", "is_email_verified", "updated_at"]
            )
    except IntegrityError:
        logger.warning(f"Email change lost the unique race | user={user.id}")
        raise EmailChangeError(EMAIL_TAKEN_MESSAGE, "email_taken")

    # Only once the write has committed. A binding dropped before the save
    # would leave a user who hit a database error with a spent code and no
    # pending change — two dead ends instead of one retryable step.
    cache_delete(CacheKeys.email_change_pending(user.id))

    # To the address being LEFT, and masked. If this change was not the
    # owner's, that inbox is the only place they will ever hear about it.
    if old_email:
        send_email_changed_notice(
            name=user.profile_name,
            email=old_email,
            new_email=mask_email(new_email),
        )

    logger.info(f"Email changed | user={user.id}")

    return new_email
