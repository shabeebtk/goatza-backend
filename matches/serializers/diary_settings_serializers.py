"""
Shaping for the player's match diary settings.

One writable field and three derived ones. The split matters: the streak
counters are FACTS about the player's matches, maintained by
``MatchService.recompute_streak``. A settings endpoint that could set them would
turn them into a claim, and a claim is worth nothing on a screen whose whole
value is that the number was earned.
"""

from rest_framework import serializers

from matches.models import MatchDiarySettings


class MatchDiarySettingsSerializer(serializers.ModelSerializer):
    """What the settings screen reads back, from GET and from PATCH alike."""

    class Meta:
        model = MatchDiarySettings
        fields = [
            "showcase_summary",
            "current_streak_weeks",
            "longest_streak_weeks",
            "last_logged_at",
            "updated_at",
        ]
        read_only_fields = [
            "current_streak_weeks",
            "longest_streak_weeks",
            "last_logged_at",
            "updated_at",
        ]


class MatchDiarySettingsUpdateSerializer(serializers.Serializer):
    """
    PATCH body.

    ``showcase_summary`` is optional and has no default: an empty body must be
    rejected rather than silently writing False and switching a player's
    showcase off. The service rejects it too — this is the earlier of the two
    gates, not the only one.
    """

    showcase_summary = serializers.BooleanField(required=False)

    def validate(self, attrs):
        if not attrs:
            raise serializers.ValidationError(
                "Nothing to update. Send showcase_summary."
            )
        return attrs
