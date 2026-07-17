"""
Helpers for turning DRF ValidationError details into structured, user-friendly
API error payloads.

DRF's ValidationError.detail can be a str, a list of ErrorDetail, a dict of
field -> list, or nested versions of those (for nested serializers). str(detail)
produces ugly reprs like "[ErrorDetail(string='...', code='invalid')]" — never
show that to a client. flatten_validation_error() extracts clean human strings.
"""


def _first_message(value):
    """Return the first leaf string found in a DRF error structure, or None."""
    # ErrorDetail is a str subclass, so this catches plain messages too.
    if isinstance(value, str):
        return str(value)

    if isinstance(value, dict):
        for item in value.values():
            message = _first_message(item)
            if message is not None:
                return message
        return None

    if isinstance(value, (list, tuple)):
        for item in value:
            message = _first_message(item)
            if message is not None:
                return message
        return None

    if value is None:
        return None

    return str(value)


# Friendly labels for field keys, so the top-level message reads like a
# sentence ("Positions: This field is required.") instead of exposing raw
# serializer keys. Anything not listed falls back to a title-cased key.
_FIELD_LABELS = {
    "sport_id": "Sport",
    "recruitment_type": "Type",
    "short_description": "Short description",
    "event_date": "Trial / event date",
    "application_deadline": "Application deadline",
    "external_apply_url": "Application link",
    "apply_method": "Apply method",
    "fee_amount": "Fee amount",
    "max_applications": "Max applications",
    "age_categories": "Age categories",
    "positions": "Positions",
    "questions": "Questions",
    "contacts": "Contacts",
    "media": "Media",
    "shared_name": "Name",
    "shared_email": "Email",
    "shared_phone": "Phone",
}


def _humanize_field(field):
    """A field key → human label ('event_date' → 'Trial / event date')."""
    if field in _FIELD_LABELS:
        return _FIELD_LABELS[field]
    return field.replace("_", " ").strip().capitalize()


def flatten_validation_error(detail):
    """
    Flatten a DRF ValidationError.detail into:

        {
            "errors": {field: "first message", ...},
            "message": "<first human-readable message>",
        }

    Field errors keep their field name; list / string details are keyed under
    "non_field_errors". The top-level message is the non_field_errors message
    when present (already a full sentence), otherwise the first FIELD message
    prefixed with the field's human label — so a bare "This field is required."
    becomes "Positions: This field is required." and the client can tell which
    input actually failed.
    """
    errors = {}

    if isinstance(detail, dict):
        for field, value in detail.items():
            message = _first_message(value)
            if message is not None:
                errors[str(field)] = message
    else:
        message = _first_message(detail)
        if message is not None:
            errors["non_field_errors"] = message

    if "non_field_errors" in errors:
        # Cross-field / object-level errors are already full sentences.
        message = errors["non_field_errors"]
    elif errors:
        # Field errors: prefix the human label so a bare "This field is
        # required." tells the client which field actually failed.
        field, first = next(iter(errors.items()))
        message = f"{_humanize_field(field)}: {first}"
    else:
        message = "Validation error"

    return {"errors": errors, "message": message}


def error_body(message, field="non_field_errors"):
    """
    Structured `data` payload for non-serializer failures (e.g. manual 400s),
    matching the shape flatten_validation_error produces: {"errors": {field: msg}}.
    Pair it with message=<message> on response_data so every error path is uniform.
    """
    return {"errors": {field: message}}
