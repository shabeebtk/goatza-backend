"""
Phone numbers, normalized to E.164 — THE one helper. The page's WhatsApp and
Call numbers use it, and so does the enquiry form (apps/enquiries), so a number
typed on the form and a number typed in the editor compare equal.

No dependency. The rules, after stripping spaces, dashes, dots and brackets:

    "+" followed by 8–15 digits          → kept as it is
    10 digits starting 6–9               → "+91" + the 10 digits
    "0"  + those 10 digits               → "+91" + the 10 digits
    "91" + those 10 digits               → "+91" + the 10 digits
    anything else                        → refused

India first because that is who uses the product; a number from anywhere else
is accepted in its international form ("+44 20 …").
"""

import re

INVALID_PHONE_MESSAGE = "Enter a valid phone number."

# Spaces (any whitespace), dashes, dots and brackets.
_SEPARATORS = re.compile(r"[\s\-.()\[\]]")
_INTERNATIONAL = re.compile(r"\+\d{8,15}")
_INDIAN_MOBILE = re.compile(r"[6-9]\d{9}")


class InvalidPhone(ValueError):
    """Not a number this helper can store. Its text is the user-facing sentence."""

    def __init__(self, message=INVALID_PHONE_MESSAGE):
        super().__init__(message)


def normalize_phone(value) -> str:
    """The number as E.164 (``"+919876543210"``), or raises InvalidPhone."""
    if not isinstance(value, str):
        raise InvalidPhone()

    raw = _SEPARATORS.sub("", value.strip())

    if raw.startswith("+"):
        if _INTERNATIONAL.fullmatch(raw):
            return raw
        raise InvalidPhone()

    for prefix in ("", "0", "91"):
        rest = raw[len(prefix):]
        if raw.startswith(prefix) and _INDIAN_MOBILE.fullmatch(rest):
            return f"+91{rest}"

    raise InvalidPhone()


def mask_phone(e164) -> str:
    """
    ``"+91 98•••• 3210"`` — enough for someone to recognise their own number,
    never enough to dial it. For an Indian number: the country code, the first
    two digits, four dots, the last four. Anything else keeps its first three
    and last four characters.
    """
    if not isinstance(e164, str) or not e164:
        return ""

    if e164.startswith("+91") and len(e164) == 13:
        local = e164[3:]
        return f"+91 {local[:2]}•••• {local[-4:]}"

    if len(e164) <= 7:
        return "•" * len(e164)
    return f"{e164[:3]}•••• {e164[-4:]}"
