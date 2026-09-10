"""
Deciding which country's rules apply to a signup, and whether it may proceed.

The reference data lives in ``accounts/constants.py``; this module is the two
pieces of judgement built on top of it. Both run on the signup paths — the
email form (``UserSignupAPIView``) and the Google role step
(``SetUserRoleAPIView``) — and nowhere else, because the answer they produce is
written to the row once and read from the row afterwards.

WHY THE DECLARED COUNTRY IS NOT SIMPLY BELIEVED

The whole minor regime keys off ``User.country_code``, and ``User.country_code``
comes from a dropdown the user fills in themselves. Left unchecked that is a
switch labelled "turn off the child-protection rules": a 15-year-old in India,
where the age of digital consent is 18, picks "United Kingdom", where it is 13,
and stops being a minor. They do not even have to be dishonest about it — the
dropdown does not explain what it is for, which is deliberate (telling users
what the field decides is telling them what to lie about).

So the declaration is cross-checked against the one other country signal that
already exists on the signup payload: the international dialling code of the
phone number. It is not proof of residence — numbers roam, and plenty of people
hold a foreign SIM — which is exactly why the rule below is asymmetric. A phone
number can only ever make the answer STRICTER, never looser. Somebody with a
+91 number who says "GB" gets India's 18; somebody with a +44 number who says
"IN" still gets India's 18. Nothing a user can put in either field lowers the
protection the other field implies.

WHAT THIS DELIBERATELY DOES NOT DO

No IP geolocation. It would be a third signal and a better one, but it is also
a new third-party dependency on the signup path, a new class of outage, a
processing purpose that would have to appear in the privacy policy, and a
stream of false positives from VPNs and carrier-grade NAT. The dialling code is
free, already in the payload, and closes the specific hole above. If IP is ever
added it belongs here, behind the same "stricter only" rule.
"""

import datetime
import logging

from rest_framework.exceptions import ValidationError

from accounts.constants import (
    EARLIEST_PLAUSIBLE_BIRTH_YEAR,
    MINIMUM_SIGNUP_AGE,
    age_on,
    country_for_dialling_code,
    minor_age_for,
    normalize_country,
)

logger = logging.getLogger(__name__)

# THE under-13 message, and the reason it says nothing.
#
# "You must be 13 or older" is a hint, not an error. It tells somebody who was
# refused precisely which number to beat, and the retry costs one page refresh
# and a different year in the same box. A rejection that explains itself is a
# rejection that trains the next attempt, so this one does not: it is the same
# neutral sentence a suspended account or a blocked device would get, and it is
# used verbatim on both signup paths so the two cannot be told apart either.
UNDER_AGE_MESSAGE = "You can't create an account right now."

# By contrast, a birthdate that is in the future or before 1900 is a TYPO, not
# an attempt, and the person who made it needs to know so they can fix it.
INVALID_BIRTHDATE_MESSAGE = "Enter a valid date of birth."


class AgeGateError(ValidationError):
    """
    A ValidationError carrying a stable machine code — see PhoneChangeError.

    The code, not the message, is what callers branch on: ``under_age`` and
    ``invalid_birthdate`` are two different situations that must stay
    distinguishable in logs and on the client even though only one of them is
    allowed to explain itself to the user.
    """

    def __init__(self, message, code):
        super().__init__(message)
        self.error_code = code


def parse_birthdate(value):
    """
    An ISO ``"YYYY-MM-DD"`` string from a request body, as a ``date``.

    Raises ``AgeGateError("invalid_birthdate")`` for anything else. Lives here
    rather than in each view because BOTH signup paths take the same field from
    the same kind of payload, and a second parser is a second chance for the
    two to disagree about what "2010-2-30" means.

    ``date.fromisoformat`` is strict on purpose: it rejects "2010-13-01" and
    "2011-02-29" outright rather than rolling them over into a neighbouring
    month the way a lenient parser would, so an impossible date can never be
    stored as a possible one.
    """
    if isinstance(value, datetime.date) and not isinstance(
        value, datetime.datetime
    ):
        return value

    if not value or not isinstance(value, str):
        raise AgeGateError(INVALID_BIRTHDATE_MESSAGE, "invalid_birthdate")

    try:
        return datetime.date.fromisoformat(value.strip())
    except ValueError:
        raise AgeGateError(INVALID_BIRTHDATE_MESSAGE, "invalid_birthdate")


def resolve_country(declared_country, phone):
    """
    The country whose rules this signup will be held to.

    Returns the STRICTER of the country the user declared and the country their
    phone's dialling code implies, "stricter" meaning the higher
    ``minor_age_for``. Returns the declared country unchanged when there is no
    phone, when the number is in national format, or when its prefix is not in
    the table — an unreadable number is no information, and no information must
    not be allowed to override an answer the user actually gave.

    Ties go to the declared country, which matters more than it looks: DE and
    NL are both 16, so a Dutch number on a German declaration leaves the user
    German rather than silently relocating them. The cross-check exists to stop
    the age threshold being gamed, not to overrule people about where they are.
    """
    declared = normalize_country(declared_country)
    implied = country_for_dialling_code(phone)

    if not implied or implied == declared:
        return declared

    if minor_age_for(implied) > minor_age_for(declared):
        logger.info(
            "Age gate | declared country overridden by dialling code | "
            f"declared={declared or '-'} implied={implied}"
        )
        return implied

    return declared


def validate_signup_age(birthdate, country_code):
    """
    Refuse a signup that must not happen. Returns None; raises ``AgeGateError``.

    Two separate refusals, in this order:

      1. **Implausible** — a birthdate in the future or before
         ``EARLIEST_PLAUSIBLE_BIRTH_YEAR``. Checked FIRST, and not only for
         tidiness: a future birthdate produces a negative age, which would sail
         straight through the under-13 test below as "too young" and hand a
         typo the under-age message instead of a fixable one. This one says
         what is wrong, because it is a slip rather than an attempt.

      2. **Under ``MINIMUM_SIGNUP_AGE``** — the floor that applies in every
         country (see the constant). The message is deliberately silent about
         why; see UNDER_AGE_MESSAGE.

    ``country_code`` is not read by either rule today — the floor is global —
    but it is in the signature because it is the thing that would change if it
    ever stopped being global, and because every caller already has it. Passing
    it means a per-country floor is a change to this function alone.
    """
    if birthdate is None:
        raise AgeGateError(INVALID_BIRTHDATE_MESSAGE, "invalid_birthdate")

    today = datetime.date.today()

    if birthdate > today or birthdate.year < EARLIEST_PLAUSIBLE_BIRTH_YEAR:
        raise AgeGateError(INVALID_BIRTHDATE_MESSAGE, "invalid_birthdate")

    if age_on(birthdate, today) < MINIMUM_SIGNUP_AGE:
        # Logged WITHOUT the birthdate. The refusal is the only thing worth
        # keeping; the date of birth of somebody who is not allowed an account
        # is personal data we have just decided not to hold.
        logger.info(
            f"Age gate | signup refused, under {MINIMUM_SIGNUP_AGE} | "
            f"country={normalize_country(country_code) or '-'}"
        )
        raise AgeGateError(UNDER_AGE_MESSAGE, "under_age")
