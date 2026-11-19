"""
Field cleaners shared by every page service. Each one takes the raw JSON value
and either returns the value to store or raises ``invalid`` naming the field,
so a form can put the sentence under the right input.

They only check ONE value. Cross-field rules (min ≤ max, end after start, a
switch that needs its number) live in the service that owns the fields,
after the payload has been merged over what is stored.
"""

import datetime
from urllib.parse import urlsplit

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator, validate_email

from apps.orgpages import constants as C
from apps.orgpages.errors import invalid, invalid_photo
from apps.orgpages.phones import InvalidPhone, normalize_phone
from services.storage.validators import allowed_image_extensions, validate_media
from utils.validations import is_valid_uuid

_https_url = URLValidator(schemes=["https"])


def clean_bool(value, field):
    if not isinstance(value, bool):
        raise invalid("Choose on or off.", field)
    return value


def clean_text(value, field, *, label, max_len, min_len=0, required=False):
    """Trimmed text. ``None`` reads as empty; empty is refused only when required."""
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise invalid(f"{label} must be text.", field)
    value = value.strip()

    if not value:
        if required:
            raise invalid(f"{label} is required.", field)
        return ""

    if not min_len <= len(value) <= max_len:
        message = (
            f"{label} is {min_len} to {max_len} characters."
            if min_len
            else f"{label} can be up to {max_len} characters."
        )
        raise invalid(message, field)
    return value


def clean_int(value, field, *, label, lo, hi, nullable=False):
    """A whole number in ``lo..hi``. ``True``/``False`` are not numbers here."""
    if value is None or value == "":
        if nullable:
            return None
        raise invalid(f"{label} is required.", field)

    if isinstance(value, bool):
        raise invalid(f"{label} must be a number.", field)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        value = int(value.strip())
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if not isinstance(value, int):
        raise invalid(f"{label} must be a whole number.", field)

    if not lo <= value <= hi:
        raise invalid(f"{label} must be between {lo} and {hi}.", field)
    return value


def clean_choice(value, field, *, label, choices, allow_blank=False):
    if value is None:
        value = ""
    if allow_blank and value == "":
        return ""
    if value not in choices:
        raise invalid(f"Choose a valid {label.lower()}.", field)
    return value


def clean_days(value, field, *, at_least_one):
    """Days from mon..sun, returned in week order with duplicates dropped."""
    if value is None:
        value = []
    if not isinstance(value, list) or not all(isinstance(day, str) for day in value):
        raise invalid("Choose days of the week.", field)

    days = {day.strip().lower() for day in value}
    unknown = days - set(C.WEEK_DAYS)
    if unknown:
        raise invalid("Choose days of the week.", field)
    if at_least_one and not days:
        raise invalid("Choose at least one day.", field)

    return [day for day in C.WEEK_DAYS if day in days]


def clean_string_list(value, field, *, label, max_items, min_len, max_len):
    """
    Trimmed strings, de-duplicated case-insensitively (the first spelling
    wins), at most ``max_items`` after de-duplication.
    """
    if value is None:
        value = []
    if not isinstance(value, list):
        raise invalid(f"{label} must be a list.", field)

    cleaned, seen = [], set()
    for item in value:
        if not isinstance(item, str):
            raise invalid(f"{label} must be text.", field)
        item = " ".join(item.split())
        if not min_len <= len(item) <= max_len:
            raise invalid(
                f"Each of these is {min_len} to {max_len} characters.", field
            )
        folded = item.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        cleaned.append(item)

    if len(cleaned) > max_items:
        raise invalid(f"You can add up to {max_items}.", field)
    return cleaned


def _host_matches(host, domains):
    return any(host == domain or host.endswith(f".{domain}") for domain in domains)


def clean_social_url(value, field):
    """Blank, or an https link on the field's own site (constants.SOCIAL_HOSTS)."""
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise invalid("Enter a link.", field)
    value = value.strip()
    if not value:
        return ""

    domains = C.SOCIAL_HOSTS[field]
    site = domains[0].split(".")[0].capitalize()
    message = f"Enter an https link to your {site} page."

    if len(value) > C.SOCIAL_URL_MAX:
        raise invalid(message, field)
    try:
        _https_url(value)
    except ValidationError:
        raise invalid(message, field)

    parts = urlsplit(value)
    host = (parts.hostname or "").lower()
    if parts.username or parts.password or parts.port or not _host_matches(host, domains):
        raise invalid(message, field)
    return value


def clean_time(value, field, *, label):
    """``"16:30"`` (or ``"16:30:00"``) → ``datetime.time``."""
    if isinstance(value, str):
        text = value.strip()
        for fmt in ("%H:%M", "%H:%M:%S"):
            try:
                return datetime.datetime.strptime(text, fmt).time()
            except ValueError:
                continue
    raise invalid(f"{label} must be a time like 16:30.", field)


def clean_phone(value, field):
    """Blank, or E.164 via phones.normalize_phone."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return ""
    try:
        return normalize_phone(value)
    except InvalidPhone as exc:
        raise invalid(str(exc), field)


def clean_email(value, field):
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise invalid("Enter a valid email address.", field)
    value = value.strip().lower()
    if not value:
        return ""
    if len(value) > 254:
        raise invalid("Enter a valid email address.", field)
    try:
        validate_email(value)
    except ValidationError:
        raise invalid("Enter a valid email address.", field)
    return value


def clean_uuid(value, field, *, message):
    if value is None or not is_valid_uuid(str(value)):
        raise invalid(message, field)
    return str(value)


def clean_ids(payload, existing_ids, field="ids"):
    """
    A reorder's ``ids``: EXACTLY the rows that exist — every one, once, no
    strangers. Anything else means the client's list is stale.
    """
    ids = payload.get(field) if isinstance(payload, dict) else None
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        raise invalid("Send the list of ids in their new order.", field)

    wanted = [i.lower() for i in ids]
    existing = {str(i).lower() for i in existing_ids}
    if len(wanted) != len(set(wanted)) or set(wanted) != existing:
        raise invalid("The list changed. Refresh and try again.", field)
    return wanted


def clean_photo(actor, organization, photo_url, photo_public_id):
    """
    ``(url, public_id)`` for a photo uploaded as type "org_page": ours, an
    image, under THIS org's own ``organizations/<id>/page/`` folder.
    """
    url = photo_url.strip() if isinstance(photo_url, str) else ""
    public_id = photo_public_id.strip() if isinstance(photo_public_id, str) else ""

    if (
        not url
        or not public_id
        or len(url) > C.PHOTO_URL_MAX
        or len(public_id) > C.PHOTO_ID_MAX
        or not public_id.startswith(C.page_photo_prefix(organization.id))
    ):
        raise invalid_photo()

    try:
        validate_media(
            actor.user,
            url,
            public_id,
            org=organization,
            allowed_extensions=allowed_image_extensions(),
        )
    except ValueError:
        raise invalid_photo()

    return url, public_id
