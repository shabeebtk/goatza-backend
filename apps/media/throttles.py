"""Throttle for queueing a media conversion."""

from apps.messaging.throttles import ActorScopedThrottle


class MediaJobThrottle(ActorScopedThrottle):
    """
    60/hour on POST /media/jobs (``media_jobs`` in DEFAULT_THROTTLE_RATES).

    Per ACTOR, like ShareThrottle: a club's media manager uploading for the
    club must not spend their personal budget, and the files land in
    different folders anyway.

    A job is the most expensive request in the app — up to 25 minutes of a
    worker's CPU — so this is a ceiling on that, not on uploads: the honest
    shape is one job per original, and only for files the browser could not
    convert itself. 60 an hour is a gallery of ten unconvertible photos six
    times over. The GET a client polls with is not counted.
    """

    scope = "media_jobs"
