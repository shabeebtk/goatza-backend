# utils/timezones.py
"""
THE calendar a recruitment runs on, and how a new one gets a sensible default.

WHY THIS EXISTS. Every datetime on this product is STORED in UTC
(``TIME_ZONE = "UTC"``, ``USE_TZ = True``) and that was never the problem.
The problem was the CALENDAR: one setting decided what "today" meant for
every recruitment everywhere, so a London trial was marked ended at 6:30pm
London time and its evening-before reminder fired at 12:30pm.

THE RULE NOW. A recruitment carries its own IANA zone
(``Recruitment.timezone``, seeded from ``Organization.timezone``), and the
timezone maths happens at WRITE time: ``_sync_trial_window`` builds
``event_date`` and ``trial_end_date`` IN THAT ZONE and stores the resulting
UTC instants. Every read is then a plain UTC comparison — see
``apps/recruitments/trial_window.py``. Nothing queries with a timezone,
because by the time a row is stored its instants are already correct for its
own country.

WHAT THIS MODULE OWNS

  * ``validate_timezone``  — the one validator. A typo must never be stored:
    the whole calendar for an org's trials hangs off this string and nothing
    downstream can tell a misspelling from a deliberate choice.
  * ``zone``               — name -> ``ZoneInfo``, defensively.
  * ``default_timezone``   — the field default, read from settings LAZILY so
    ``override_settings`` works and so the migration stores a reference
    rather than a baked-in value.
  * ``COUNTRY_TIMEZONES``  — a suggestion for a new org, from its country.

``settings.RECRUITMENT_TIMEZONE`` is read HERE and nowhere else. It is the
default for a new org, not the rule for anybody's trial.
"""

from functools import lru_cache
from zoneinfo import ZoneInfo, available_timezones

from django.conf import settings
from django.core.exceptions import ValidationError

# Column width. The longest IANA name tzdata ships is well under this
# ("America/Argentina/ComodRivadavia", 32 characters), and 64 leaves room for
# whatever it adds next without another migration.
TIMEZONE_MAX_LENGTH = 64


@lru_cache(maxsize=1)
def _known_timezones():
    """
    Every IANA name this interpreter's tzdata knows, as a frozenset.

    Cached because ``available_timezones()`` walks the zoneinfo tree on every
    call and this sits on the write path of every recruitment.
    """
    return frozenset(available_timezones())


def default_timezone():
    """
    The zone a brand-new organization starts on.

    A CALLABLE, not the value: as a field default it is evaluated per row, so
    the setting can be changed (or overridden in a test) without a migration,
    and the migration serializes a reference to this function rather than
    baking "Asia/Kolkata" into the schema.
    """
    return settings.RECRUITMENT_TIMEZONE


def validate_timezone(value):
    """
    Raise unless ``value`` is an IANA timezone name this machine knows.

    Used as a model field validator AND called directly from ``save()`` on
    both models that carry one — a field validator alone only runs under
    ``full_clean()``, and "a typo cannot be stored" has to be true of every
    write path, not only the ones that happen to validate.
    """
    if not value:
        raise ValidationError("A timezone is required.")

    if value not in _known_timezones():
        raise ValidationError(
            f"{value} is not a valid timezone. Use an IANA name such as "
            f"Asia/Kolkata or Europe/London."
        )

    return value


def zone(name):
    """
    ``ZoneInfo`` for a stored name, falling back to the default.

    The fallback is for READS only, and it is deliberately quiet: every write
    path validates, so an unknown name here means tzdata shrank under a row
    that was legal when it was written. Answering with the default beats
    raising on a list endpoint — the trial reads a few hours out, the page
    still loads.
    """
    if name and name in _known_timezones():
        return ZoneInfo(name)
    return ZoneInfo(default_timezone())


# ---------------------------------------------------------------------
# SUGGESTING A DEFAULT
# ---------------------------------------------------------------------
#
# UNAMBIGUOUS COUNTRIES ONLY. An org's country is a good hint and a terrible
# guess: a club in Denver and a club in New York share "US" and not a clock,
# and a wrong guess is worse than a sensible default they can change — it
# looks authoritative, so nobody checks it.
#
# A country is in here when the WHOLE country keeps one clock. That includes
# three where tzdata carries more than one identifier for a single legal time
# (CN: Asia/Urumqi; DE: Europe/Busingen; MY: Asia/Kuching) — what matters
# here is the clock, not the identifier count.
#
# DELIBERATELY ABSENT, and they must stay absent: US, CA, AU, BR, RU, MX, ID,
# KZ, CL, EC, AR, ES, PT, NZ, UA, CD. Each spans real offsets (or a disputed
# zone), so an org there picks its own and gets the settings default until it
# does.
COUNTRY_TIMEZONES = {
    # South Asia
    "IN": "Asia/Kolkata",
    "LK": "Asia/Colombo",
    "BD": "Asia/Dhaka",
    "PK": "Asia/Karachi",
    "NP": "Asia/Kathmandu",
    "BT": "Asia/Thimphu",
    "MV": "Indian/Maldives",
    "AF": "Asia/Kabul",

    # Gulf and Middle East
    "AE": "Asia/Dubai",
    "SA": "Asia/Riyadh",
    "QA": "Asia/Qatar",
    "KW": "Asia/Kuwait",
    "BH": "Asia/Bahrain",
    "OM": "Asia/Muscat",
    "JO": "Asia/Amman",
    "LB": "Asia/Beirut",
    "IQ": "Asia/Baghdad",
    "IL": "Asia/Jerusalem",
    "IR": "Asia/Tehran",
    "TR": "Europe/Istanbul",

    # East and South-East Asia
    "CN": "Asia/Shanghai",
    "HK": "Asia/Hong_Kong",
    "MO": "Asia/Macau",
    "TW": "Asia/Taipei",
    "JP": "Asia/Tokyo",
    "KR": "Asia/Seoul",
    "SG": "Asia/Singapore",
    "MY": "Asia/Kuala_Lumpur",
    "TH": "Asia/Bangkok",
    "VN": "Asia/Ho_Chi_Minh",
    "PH": "Asia/Manila",
    "KH": "Asia/Phnom_Penh",
    "LA": "Asia/Vientiane",
    "MM": "Asia/Yangon",
    "BN": "Asia/Brunei",

    # Europe
    "GB": "Europe/London",
    "IE": "Europe/Dublin",
    "FR": "Europe/Paris",
    "DE": "Europe/Berlin",
    "IT": "Europe/Rome",
    "NL": "Europe/Amsterdam",
    "BE": "Europe/Brussels",
    "LU": "Europe/Luxembourg",
    "CH": "Europe/Zurich",
    "AT": "Europe/Vienna",
    "SE": "Europe/Stockholm",
    "NO": "Europe/Oslo",
    "DK": "Europe/Copenhagen",
    "FI": "Europe/Helsinki",
    "IS": "Atlantic/Reykjavik",
    "PL": "Europe/Warsaw",
    "CZ": "Europe/Prague",
    "SK": "Europe/Bratislava",
    "HU": "Europe/Budapest",
    "SI": "Europe/Ljubljana",
    "HR": "Europe/Zagreb",
    "RS": "Europe/Belgrade",
    "BA": "Europe/Sarajevo",
    "MK": "Europe/Skopje",
    "AL": "Europe/Tirane",
    "GR": "Europe/Athens",
    "BG": "Europe/Sofia",
    "RO": "Europe/Bucharest",
    "MD": "Europe/Chisinau",
    "EE": "Europe/Tallinn",
    "LV": "Europe/Riga",
    "LT": "Europe/Vilnius",
    "BY": "Europe/Minsk",
    "MT": "Europe/Malta",
    "CY": "Asia/Nicosia",
    "GE": "Asia/Tbilisi",
    "AM": "Asia/Yerevan",
    "AZ": "Asia/Baku",

    # Africa
    "ZA": "Africa/Johannesburg",
    "NG": "Africa/Lagos",
    "GH": "Africa/Accra",
    "KE": "Africa/Nairobi",
    "TZ": "Africa/Dar_es_Salaam",
    "UG": "Africa/Kampala",
    "RW": "Africa/Kigali",
    "ET": "Africa/Addis_Ababa",
    "EG": "Africa/Cairo",
    "MA": "Africa/Casablanca",
    "TN": "Africa/Tunis",
    "DZ": "Africa/Algiers",
    "LY": "Africa/Tripoli",
    "SN": "Africa/Dakar",
    "CI": "Africa/Abidjan",
    "CM": "Africa/Douala",
    "ZW": "Africa/Harare",
    "ZM": "Africa/Lusaka",
    "MU": "Indian/Mauritius",

    # Americas — single-offset countries only
    "PE": "America/Lima",
    "CO": "America/Bogota",
    "VE": "America/Caracas",
    "BO": "America/La_Paz",
    "PY": "America/Asuncion",
    "UY": "America/Montevideo",
    "CR": "America/Costa_Rica",
    "PA": "America/Panama",
    "GT": "America/Guatemala",
    "JM": "America/Jamaica",
    "TT": "America/Port_of_Spain",
    "DO": "America/Santo_Domingo",
    "CU": "America/Havana",

    # Oceania — NZ and AU are deliberately out, see above
    "FJ": "Pacific/Fiji",
    "PG": "Pacific/Port_Moresby",
}


def timezone_for_country(country_code):
    """
    The zone to SUGGEST for an org in this country, or the settings default.

    A suggestion, never a decision: the org can change it in its settings,
    and a country that keeps more than one clock is not in the map at all.
    """
    if not country_code:
        return default_timezone()

    return COUNTRY_TIMEZONES.get(
        country_code.strip().upper(), default_timezone()
    )
