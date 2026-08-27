"""Throttle for the one user-facing moderation write."""

from rest_framework.throttling import UserRateThrottle


class ReportThrottle(UserRateThrottle):
    """
    10/hour on filing reports (``moderation_report`` in DEFAULT_THROTTLE_RATES).

    Per USER, not per actor — the one place in the app that deliberately
    breaks from messaging.throttles.ActorScopedThrottle. An actor-scoped bucket
    would hand one person a fresh ten reports for every org they are a member
    of, which is precisely the leverage a brigading account wants. The budget
    belongs to the human behind the headers.

    Tight because reporting is cheap to send and expensive to review: ten an
    hour is far more than anyone browsing in good faith reaches, and far less
    than a script needs to bury a moderator. Nothing is lost when it trips —
    the reports already filed still stand, and re-reporting an open target was
    a no-op anyway (see ReportService dedup).
    """

    scope = "moderation_report"
