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


def flatten_validation_error(detail):
    """
    Flatten a DRF ValidationError.detail into:

        {
            "errors": {field: "first message", ...},
            "message": "<first human-readable message>",
        }

    Field errors keep their field name; list / string details are keyed under
    "non_field_errors". The top-level message is the non_field_errors message
    when present, otherwise the first field message.
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
        message = errors["non_field_errors"]
    elif errors:
        message = next(iter(errors.values()))
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
