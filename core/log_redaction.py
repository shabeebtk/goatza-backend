"""
Keeps token links out of logs and Sentry. A guardian consent token or an org
invite token in a URL is a credential: anyone reading the log line can use it.

Imported by core/settings.py, so nothing here may touch Django.
"""

import logging
import re

REDACTED = "[redacted]"

# The API routes that take a token, and the two pages an email links to. The
# frontend's own /guardian/<token> shares a prefix with /guardian/details and
# /guardian/resend, which carry nothing.
_TOKEN_PATH = re.compile(
    r"(/organizations/invites/token/"
    r"|/guardian/consent/"
    r"|/org-invite/"
    r"|/guardian/(?!consent/|details\b|resend\b))"
    r"[^/?#\s\"'<>]+"
)

# A frame's local variables reach Sentry by name. Its own scrubber knows `token`
# but not this one, and the value alone has no path around it to match on.
_SECRET_KEYS = frozenset({"raw_token"})


def redact_token_paths(text):
    return _TOKEN_PATH.sub(lambda match: f"{match.group(1)}{REDACTED}", text)


def _redact(value):
    if isinstance(value, str):
        return redact_token_paths(value)

    if isinstance(value, dict):
        return {
            key: REDACTED if key in _SECRET_KEYS else _redact(item)
            for key, item in value.items()
        }

    if isinstance(value, (list, tuple)):
        return type(value)(_redact(item) for item in value)

    return value


def sentry_redact_event(event, hint):
    """
    The whole event rather than the named fields: request.url, the transaction,
    exception messages, breadcrumbs, frame variables and a task's arguments (which
    hold a whole email body) can all carry a link.
    """
    return _redact(event)


def sentry_redact_breadcrumb(crumb, hint):
    return _redact(crumb)


class RedactTokenPathsFilter(logging.Filter):
    """
    Attached to the console HANDLER, not to django.request and django.server. A
    logger's filter skips its children and every other logger, and under
    ``manage.py runserver`` the access log is django.channels.server.

    It CANNOT see production's access log. The ``daphne`` command writes that
    straight to sys.stdout, not through logging at all, which is why production
    runs it with ``-v 0`` (see "Web service" in README.md).
    """

    def filter(self, record):
        try:
            message = record.getMessage()
        except Exception:
            # A bad format string is logging's own error to report.
            return True

        redacted = redact_token_paths(message)
        if redacted != message:
            record.msg = redacted
            record.args = ()

        return True
