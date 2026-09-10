"""
The age gate's reference data, in one place.

WHY THIS IS A TABLE AND NOT A NUMBER

"Minor" is not a fact about a person, it is a fact about a person IN A
JURISDICTION. The same fifteen-year-old is a child in India, where the DPDP Act
puts the age of digital consent at 18, and old enough to consent for themselves
in the UK, where the UK GDPR sets it at 13. There is no single correct
threshold to hard-code, so the threshold is looked up per country and every
caller goes through ``minor_age_for`` rather than comparing against a literal.

Nothing in the product reads these yet. They exist so that when something does
-- parental consent, DM restrictions, discoverability, ad targeting -- it reads
one table instead of inventing a second one.

WHAT THIS MODULE DELIBERATELY DOES NOT MODEL

Australia's under-16 rule is an account PROHIBITION, not an age of digital
consent, and the two are different rules with different consequences: a consent
age says "a guardian must agree", a prohibition says "there is no account, with
or without a guardian". AU sits in the table at 16 because that is the age
below which an Australian user needs special handling, but reading that 16 as a
consent age would be wrong. The prohibition is NOT implemented here -- doing so
needs a decision about existing Australian accounts that nobody has made yet.
Do not read AU's row as "Australia is handled".
"""

from __future__ import annotations

import datetime


# ISO-3166-1 alpha-2 -> age of digital consent in that jurisdiction.
#
# Only countries somebody has actually researched belong in here. A country is
# added when its rule is known, NOT when its users show up -- an unresearched
# guess in this table is worse than no row at all, because a row reads as an
# answer while a missing row falls through to the strict default below.
MINOR_AGE_BY_COUNTRY = {
    "IN": 18,  # DPDP Act 2023 -- a "child" is under 18, parental consent required
    "GB": 13,  # UK GDPR / Data Protection Act 2018 s.9
    "US": 13,  # COPPA
    "DE": 16,  # GDPR Art. 8 default, not lowered
    "NL": 16,  # GDPR Art. 8 default, not lowered
    "AU": 16,  # see the module docstring -- a prohibition, not a consent age
}

# The answer for every country not in the table above.
#
# DELIBERATELY THE STRICTEST value in the table, not an average and not the
# GDPR floor. The default is what a market nobody has looked at yet gets, and
# the two failure modes are not symmetric: defaulting low means the first users
# in a new country are silently treated as adults until somebody notices, while
# defaulting high means they get the careful treatment until somebody researches
# the real number and adds a row. Launching into a country is a decision; being
# safe there before anyone has made that decision should not be.
DEFAULT_MINOR_AGE = 18

# The floor, everywhere, for everyone. Below this there is no account to gate:
# no jurisdiction in the table lets a 12-year-old consent for themselves, and
# there is no parental-consent flow to fall back on, so the only correct answer
# is not to create the account. Independent of country on purpose -- this is
# not a per-market tunable.
MINIMUM_SIGNUP_AGE = 13

# The oldest birth year anyone will be allowed to claim. Not an age policy --
# a typo filter. "1899" and "0202" are keyboard slips, not centenarians, and
# letting them through would store a birthdate that reads as valid forever.
EARLIEST_PLAUSIBLE_BIRTH_YEAR = 1900

# International dialling code -> the country it is read as.
#
# A deliberately small, hand-kept table rather than a phonenumbers dependency:
# the only thing it feeds is ``age_service.resolve_country``, which uses it as a
# CROSS-CHECK on a self-declared country and falls back to the declaration when
# the code is unknown. A missing row therefore costs nothing, which is the only
# reason one is allowed to be missing.
#
# The "1" row is the honest limitation: +1 is the NANP, shared by the US, Canada
# and twenty-odd Caribbean states, and this table calls all of them US. That is
# survivable here for exactly one reason -- US carries the LOWEST age in
# MINOR_AGE_BY_COUNTRY, so a +1 number can never make resolve_country stricter
# than what the user declared, which means the shortcut can never cost a user
# their correct jurisdiction. If a NANP country with a HIGHER age is ever added
# to the table, this becomes a hole and has to be replaced with real parsing.
COUNTRY_BY_DIALLING_CODE = {
    "1": "US",
    "7": "RU",
    "20": "EG",
    "27": "ZA",
    "31": "NL",
    "32": "BE",
    "33": "FR",
    "34": "ES",
    "39": "IT",
    "41": "CH",
    "43": "AT",
    "44": "GB",
    "45": "DK",
    "46": "SE",
    "47": "NO",
    "48": "PL",
    "49": "DE",
    "51": "PE",
    "52": "MX",
    "54": "AR",
    "55": "BR",
    "56": "CL",
    "57": "CO",
    "60": "MY",
    "61": "AU",
    "62": "ID",
    "63": "PH",
    "64": "NZ",
    "65": "SG",
    "66": "TH",
    "81": "JP",
    "82": "KR",
    "84": "VN",
    "86": "CN",
    "90": "TR",
    "91": "IN",
    "92": "PK",
    "94": "LK",
    "212": "MA",
    "233": "GH",
    "234": "NG",
    "254": "KE",
    "351": "PT",
    "353": "IE",
    "358": "FI",
    "880": "BD",
    "966": "SA",
    "968": "OM",
    "971": "AE",
    "973": "BH",
    "974": "QA",
    "977": "NP",
}

# Longest first, so "+91" is never read as "+9" and "+353" never as "+35".
# Precomputed at import because this runs on the signup path.
_DIALLING_CODES_LONGEST_FIRST = sorted(
    COUNTRY_BY_DIALLING_CODE, key=len, reverse=True
)


def normalize_country(country_code):
    """
    The stored form of a country: uppercase alpha-2, or ``""`` for "not given".

    Every write and every lookup goes through this, so "in", "IN" and " in "
    can never become three different jurisdictions in the same database.
    Anything that is not two letters is not a country code and comes back "" --
    callers decide whether that is a 400 or simply "no information".
    """
    if not country_code or not isinstance(country_code, str):
        return ""

    code = country_code.strip().upper()

    if len(code) != 2 or not code.isalpha():
        return ""

    return code


def minor_age_for(country_code):
    """
    The age below which a user in ``country_code`` is a minor.

    Unknown, blank and unresearched countries all get ``DEFAULT_MINOR_AGE``.
    That is the entire point of having a default -- see its comment above.
    """
    return MINOR_AGE_BY_COUNTRY.get(
        normalize_country(country_code), DEFAULT_MINOR_AGE
    )


def country_for_dialling_code(phone):
    """
    The country implied by ``phone``'s international dialling code, or ``""``.

    Returns "" for anything it cannot read with confidence: no number, a
    national-format number with no "+", or a prefix that is not in the table.
    Callers must treat "" as "no information", never as a country.
    """
    if not phone or not isinstance(phone, str):
        return ""

    digits = phone.strip()

    # No "+" means there is no country code to read. A bare "9876543210" is an
    # Indian mobile to an Indian user and something else entirely to anyone
    # else; guessing here would be a guess about the user's country, which is
    # the exact thing resolve_country exists in order to stop trusting.
    if not digits.startswith("+"):
        return ""

    digits = digits[1:]

    if not digits.isdigit():
        return ""

    for code in _DIALLING_CODES_LONGEST_FIRST:
        if digits.startswith(code):
            return COUNTRY_BY_DIALLING_CODE[code]

    return ""


def age_on(birthdate, today=None):
    """
    Whole years between ``birthdate`` and ``today`` (default: today).

    The subtraction is the boring part; the tuple comparison is the part that
    matters. Comparing (month, day) means somebody whose birthday falls later
    this year has not had it yet, which is what stops a 12-year-old born in
    December from reading as 13 all through the preceding January.
    """
    if birthdate is None:
        return None

    if today is None:
        today = datetime.date.today()

    return (
        today.year
        - birthdate.year
        - ((today.month, today.day) < (birthdate.month, birthdate.day))
    )


def is_minor(birthdate, country_code):
    """
    Whether a person born on ``birthdate`` is a minor in ``country_code``.

    ``birthdate=None`` returns **True**, and that is a decision rather than a
    missing case. Unknown age has to read as a minor: this answer will
    eventually decide who can be messaged, discovered and advertised to, and in
    every one of those being wrong about a child costs far more than being
    over-careful with an adult. "No data" is not hypothetical either -- the
    column stays nullable for rows that predate the signup requirement, so
    every pre-existing account arrives here with birthdate=None.
    """
    if birthdate is None:
        return True

    return age_on(birthdate) < minor_age_for(country_code)
