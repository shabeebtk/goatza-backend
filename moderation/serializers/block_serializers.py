"""
Wire shapes for the blocked list.

The blocked identity goes through the SHARED ActorMiniSerializer rather than a
local shape: a blocked account is rendered by the same avatar/name/handle row
the client already uses for actors everywhere else, so "Unblock" screens reuse
the same list item component as followers, likes and mentions.
"""

from rest_framework import serializers

from shared.serializers.actor_serializers import ActorMiniSerializer


class BlockedItemSerializer(serializers.Serializer):
    """
    One row of GET /moderation/blocked.

    Only the BLOCKED side is exposed — the blocker is always the caller, so
    echoing it back is noise the client would have to ignore.
    """

    id = serializers.UUIDField()
    created_at = serializers.DateTimeField()
    blocked = serializers.SerializerMethodField()

    def get_blocked(self, obj):
        # Exactly one of the two is set — the model's blocked_user_or_org
        # CheckConstraint guarantees it, so no None case to handle here.
        entity = obj.blocked_user or obj.blocked_org

        return ActorMiniSerializer(entity).data
