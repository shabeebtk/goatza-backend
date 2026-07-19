from rest_framework.throttling import SimpleRateThrottle


class ActorScopedThrottle(SimpleRateThrottle):
    """
    Rate-limits per ACTOR, not per user.

    UserRateThrottle keys on the user's pk, which would make one person's org
    actions and personal actions drain the same bucket. The dual-actor rule
    says those are different actors, so they get different buckets.

    The actor is read from the headers rather than request.actor: throttling
    runs inside DRF's initial() *before* ActorMixin resolves the actor, so
    request.actor does not exist yet. Membership is unverified at this point —
    harmless, because an unverified org header only buys the caller a bucket of
    their own, and resolve_actor rejects the request moments later anyway.

    Subclasses set `scope`.
    """

    def get_cache_key(self, request, view):
        user = request.user

        if not user or not user.is_authenticated:
            # Anonymous callers can't reach these views (IsAuthenticated), but
            # fall back to IP rather than a shared "None" bucket.
            return self.cache_format % {
                "scope": self.scope,
                "ident": self.get_ident(request),
            }

        actor_type = request.headers.get("X-Actor-Type", "user")
        actor_id = request.headers.get("X-Actor-Id", "")

        if actor_type == "organization" and actor_id:
            ident = f"{user.pk}:org:{actor_id}"
        else:
            ident = f"{user.pk}:user"

        return self.cache_format % {"scope": self.scope, "ident": ident}


class ShareThrottle(ActorScopedThrottle):
    scope = "message_share"


class ChatMediaThrottle(ActorScopedThrottle):
    scope = "chat_media"
