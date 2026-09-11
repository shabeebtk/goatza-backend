from rest_framework.throttling import UserRateThrottle


class FeedImpressionThrottle(UserRateThrottle):
    """
    Own bucket for impression flushes.

    Impressions are per PERSON, so this keys on the user rather than the actor
    (unlike messaging.throttles.ActorScopedThrottle). The point of the separate
    scope is isolation: a long scroll flushes every ~10 posts, and on the shared
    'user' bucket that telemetry would eat the budget real actions need.
    """

    scope = "feed_impressions"
