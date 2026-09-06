"""
Changing the phone number — v1, deliberately unverified.

WHY THIS IS NOT THE EMAIL FLOW

Its neighbour ``email_change_service`` is a two-step, password-gated,
OTP-proved ceremony because ``email`` is ``USERNAME_FIELD``: it signs you in
and it receives password resets. ``phone`` does neither. The login view
(``UserLoginAPIView``) authenticates by email and password only, nothing mails
or texts a code to this column, and no permission anywhere reads it. It is a
contact detail, so v1 is a plain authenticated write.

THE ONE RULE THAT IS NOT OBVIOUS

Every change flips ``is_phone_verified`` back to False. Nothing sets it True
today, so this looks like a no-op — and it is, right up until SMS verification
ships, at which point a number that was verified in March and quietly replaced
in April would still be claiming it was verified. Stale trust flags are worse
than no flags: they are read as proof by code written later, by people who
were not here for this decision. Resetting on write means the flag can only
ever mean "this number, verified".

Two guards on top of that, both from the schema:

  * ``phone`` is ``unique=True``. Checked before the write for a clean message,
    and the IntegrityError is caught for the race the check cannot close.
  * ``user_email_or_phone_required`` — a CheckConstraint demanding at least one
    of the two. Clearing the phone of an account with no email would violate
    it, so that is refused with a sentence rather than a 500.
"""

import logging

from django.db import IntegrityError, transaction
from rest_framework.exceptions import ValidationError

from accounts.models import User
from utils.validations import is_valid_phone

logger = logging.getLogger(__name__)

INVALID_PHONE_MESSAGE = (
    "Enter a valid phone number — 8 to 15 digits, optionally starting with +."
)

PHONE_TAKEN_MESSAGE = "This phone number is already in use."

# The CheckConstraint's message in human form. Said only when clearing, since
# that is the only operation that can leave a row with neither.
PHONE_REQUIRED_MESSAGE = (
    "You can't remove your phone number because it's the only way to sign in "
    "to this account. Add an email address first."
)


class PhoneChangeError(ValidationError):
    """A ValidationError carrying a stable machine code — see EmailChangeError."""

    def __init__(self, message, code):
        super().__init__(message)
        self.error_code = code


def _clean_phone(phone):
    """
    Normalize the requested value to what will be stored, or ``None``.

    None and "" both mean REMOVE. They are collapsed here so the rest of the
    function has one representation of "no phone" — and it is None, not "",
    because the column is ``unique=True`` and two accounts storing an empty
    string would collide with each other.
    """
    if phone is None:
        return None

    if not isinstance(phone, str):
        raise PhoneChangeError(INVALID_PHONE_MESSAGE, "invalid_phone")

    phone = phone.strip()
    if not phone:
        return None

    if not is_valid_phone(phone):
        raise PhoneChangeError(INVALID_PHONE_MESSAGE, "invalid_phone")

    return phone


def change_phone(user, phone):
    """
    Set, replace or remove the caller's phone number.

    Returns ``{"phone": <str|None>, "is_phone_verified": False}``.

    Idempotent on a no-op: re-saving the number already on file still returns
    success, because the client asked for a state and that state is what it
    gets. The verified flag is written regardless, which costs nothing and
    keeps the invariant "this column was set by this function, so the flag
    below it is False" true without exception.
    """
    phone = _clean_phone(phone)

    if phone is None and not user.email:
        raise PhoneChangeError(PHONE_REQUIRED_MESSAGE, "phone_required")

    if phone is not None:
        if User.objects.filter(phone=phone).exclude(pk=user.pk).exists():
            raise PhoneChangeError(PHONE_TAKEN_MESSAGE, "phone_taken")

    user.phone = phone
    # DELIBERATE, and the reason this is a service rather than a serializer
    # field: a number that changed has not been verified, whatever the flag
    # used to say. Revisit when SMS OTP ships — at that point this becomes
    # "False until the code comes back" rather than "False, always".
    user.is_phone_verified = False

    try:
        with transaction.atomic():
            user.save(
                update_fields=["phone", "is_phone_verified", "updated_at"]
            )
    except IntegrityError:
        # Lost the race between the check above and the insert. The unique
        # constraint is the arbiter, and this is what it said.
        logger.info(f"Phone change lost the unique race | user={user.id}")
        raise PhoneChangeError(PHONE_TAKEN_MESSAGE, "phone_taken")

    logger.info(f"Phone changed | user={user.id} | cleared={phone is None}")

    return {"phone": user.phone, "is_phone_verified": user.is_phone_verified}
