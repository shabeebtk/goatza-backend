"""
Shaping the body of POST /app/events.

A plain function rather than a DRF Serializer because only two things here are
errors — a ``device_id`` that is not a UUID and an ``event`` we do not know.
Everything else is a bucket: an unknown platform or browser is ``other`` and an
unknown or missing source is ``unknown``. A client that learns to report a new
browser before we add it must still be counted, not refused.
"""

import uuid
from dataclasses import dataclass

from apps.pwa.models import AppEvent, Browser, InstallSource, Platform


@dataclass(frozen=True)
class ParsedAppEvent:
    device_id: uuid.UUID
    event: str
    platform: str
    browser: str
    source: str


def _bucket(value, choices, fallback):
    """``value`` when it is one of ``choices``, otherwise ``fallback``."""
    if isinstance(value, str):
        value = value.strip().lower()
        if value in choices.values:
            return value
    return fallback


def _device_id(value):
    """The UUID, or None. Only a string is accepted — never an int or a list."""
    if not isinstance(value, str):
        return None
    try:
        return uuid.UUID(value.strip())
    except ValueError:
        return None


def parse_app_event(data):
    """
    The normalized event, or None when the body is invalid (400 ``invalid``).
    """
    if not hasattr(data, "get"):
        return None

    device_id = _device_id(data.get("device_id"))
    if device_id is None:
        return None

    event = data.get("event")
    event = event.strip().lower() if isinstance(event, str) else None
    if event not in AppEvent.values:
        return None

    return ParsedAppEvent(
        device_id=device_id,
        event=event,
        platform=_bucket(data.get("platform"), Platform, Platform.OTHER),
        browser=_bucket(data.get("browser"), Browser, Browser.OTHER),
        source=_bucket(data.get("source"), InstallSource, InstallSource.UNKNOWN),
    )
