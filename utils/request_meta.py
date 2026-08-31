"""
The two pieces of request context worth storing on an audit record.

Lives in ``utils`` rather than in ``legal`` because two apps capture it: the
legal accept endpoint and the signup view, which records consent at the moment
the account is created. A helper one app imports from another app is a helper
in the wrong place.

Deliberately NOT the same function as ``cv.services.cv_services.client_ident``.
That one identifies an anonymous CV reader for a cache key and must always
return something, so it falls back to the string ``"unknown"``. This one feeds
a ``GenericIPAddressField``, where ``"unknown"`` is not a value — it is a
DataError on Postgres. Returning None is the whole difference.
"""

from django.core.exceptions import ValidationError
from django.core.validators import validate_ipv46_address


def client_ip(request):
    """
    The caller's IP address, or None when there isn't a usable one.

    ``X-Forwarded-For`` first: the app runs behind a proxy on Render, where
    ``REMOTE_ADDR`` is the proxy for every single request and would make the
    audit trail say the same thing about everybody. The left-most entry is the
    original client, the rest are the proxies it passed through.

    THE VALUE IS VALIDATED BEFORE IT IS RETURNED, and that is not tidiness.
    ``X-Forwarded-For`` is attacker-controlled — anyone can send anything — and
    it lands in an ``inet`` column. Unvalidated, a header of "'; drop" is not a
    bad audit row, it is an exception thrown inside the signup transaction,
    which is a header that stops people from creating accounts.

    A forged but well-formed address still gets stored. That is accepted: the
    IP is corroborating context on a consent record, not proof of identity, and
    the acceptance itself is authenticated by other means.
    """
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    candidate = forwarded.split(",")[0].strip() if forwarded else ""

    if not candidate:
        candidate = (request.META.get("REMOTE_ADDR") or "").strip()

    if not candidate:
        return None

    try:
        validate_ipv46_address(candidate)
    except ValidationError:
        return None

    return candidate


def client_user_agent(request):
    """
    The ``User-Agent`` header, or "". Never None — the column is ``blank=True``,
    not nullable, and the service truncates it to fit.
    """
    return request.META.get("HTTP_USER_AGENT", "") or ""
