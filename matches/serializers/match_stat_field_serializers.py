"""
Shaping for the per-sport stat catalog — what the quick-add form is built from.

Read-only throughout: the catalog is admin-seeded (``seed_match_stat_fields``),
never client-written, for the same reason ``SportAttribute`` is.
"""

from rest_framework import serializers

from matches.models import SportMatchStatField


class MatchStatFieldSerializer(serializers.ModelSerializer):
    """
    One loggable stat.

    ``is_primary`` is what the form actually branches on: the primary fields are
    the ones shown by default and the rest sit behind "add more".

    ``position_ids`` is a flat list of ids rather than nested objects. Nothing
    filters on positions in v1 (see the model), so the client gets the links now
    and can start filtering the form the moment that rule lands — without this
    endpoint changing shape or the client making a second call to find out which
    stats belong to a keeper. An empty list means the stat applies to every
    position, which is how most of them ship.
    """

    position_ids = serializers.SerializerMethodField()

    class Meta:
        model = SportMatchStatField
        fields = [
            "id",
            "name",
            "short_label",
            "unit",
            "value_type",
            "is_primary",
            "order",
            "position_ids",
        ]
        read_only_fields = fields

    def get_position_ids(self, obj):
        # Reads the prefetch active_stat_fields() sets up — never a query per row.
        return [str(position.id) for position in obj.positions.all()]
