"""
Request/response shaping for the Match Diary.

Shape only. Every rule — ownership, the sport ↔ position and sport ↔ stat_field
checks, the scheduled/played invariant, replace-on-update for stats — lives in
``MatchService``, so the shell, the admin and any future internal caller get the
same guarantees the API does. What is here is types, ranges and required-ness,
and nothing that needs a second model to decide.

``visibility`` appears nowhere in the write serializers. Not as a field, not as
read_only, not as anything a client could name. See ``MatchEntry.visibility``:
the column ships so that opening the diary up later is a serializer change
rather than a migration, and the whole point is that nothing logged in v1
becomes visible retroactively when that day comes.
"""

from rest_framework import serializers

from django.utils import timezone

from matches.models import MatchEntry, MatchEntryStat
from sports.serializers.sports_serializers import (
    SportPositionSerializer,
    SportSerializer,
)


# =====================================================================
# READ
# =====================================================================

class MatchEntryStatSerializer(serializers.ModelSerializer):
    """
    One logged stat, flattened with the catalog row it points at.

    Flattened rather than nested because the diary row prints "G 2 · A 1" and
    would otherwise have to reach two levels down for a two-character label.
    """

    stat_field_id = serializers.UUIDField(source="stat_field.id", read_only=True)
    name = serializers.CharField(source="stat_field.name", read_only=True)
    short_label = serializers.CharField(
        source="stat_field.short_label", read_only=True
    )
    unit = serializers.CharField(source="stat_field.unit", read_only=True)
    value_type = serializers.CharField(
        source="stat_field.value_type", read_only=True
    )
    # Carried so a collapsed diary row can pick its ONE headline stat without
    # fetching the sport's whole catalog to find out which of the twelve
    # matters. Read off the prefetched stat_field, so it costs nothing.
    is_primary = serializers.BooleanField(
        source="stat_field.is_primary", read_only=True
    )

    class Meta:
        model = MatchEntryStat
        fields = [
            "stat_field_id",
            "name",
            "short_label",
            "unit",
            "value_type",
            "is_primary",
            "value",
        ]
        read_only_fields = fields


class MatchCareerEntrySerializer(serializers.Serializer):
    """
    The compact career stint a match hangs off — enough for the row to say
    "during your spell at Riverside FC" without a second request.

    Four fields, deliberately. The full ``CareerEntrySerializer`` carries dates,
    positions, squad level and a nested organization, none of which a diary row
    prints; pulling it in would make every match page pay for the career page.
    ``verification_status`` is here because a verified stint is worth a tick
    next to the match, and that is a property of the stint, not of the match.
    """

    id = serializers.UUIDField(read_only=True)
    title = serializers.CharField(read_only=True)
    organization_name = serializers.CharField(read_only=True)
    verification_status = serializers.CharField(read_only=True)


class MatchEntrySerializer(serializers.ModelSerializer):
    """
    One match, as the diary list and the detail screen read it.

    Owner-only by construction: every selector that feeds this is scoped to the
    requesting player, so there is no viewer-dependent branch here and no
    ``visibility`` on the way out either.
    """

    sport = SportSerializer(read_only=True)
    position = SportPositionSerializer(read_only=True)
    stats = MatchEntryStatSerializer(many=True, read_only=True)
    career_entry = MatchCareerEntrySerializer(read_only=True)

    class Meta:
        model = MatchEntry
        fields = [
            "id",
            "sport",
            "status",
            "date",
            "kickoff_time",
            "opponent_name",
            "match_type",
            "result",
            "minutes_played",
            "position",
            "self_rating",
            "notes",
            "career_entry",
            "photo_url",
            "stats",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class UpcomingMatchSerializer(serializers.ModelSerializer):
    """
    A fixture in the "Up next" strip — deliberately lighter than the full entry.

    No result, minutes, rating, notes or stats: a scheduled match cannot have
    any of them (see ``match_entry_scheduled_has_no_result``), so returning them
    as a row of nulls would only invite a client to render empty chips.

    ``is_overdue`` is the one computed field and the reason this strip exists on
    the profile at all: a fixture whose date has passed and that was never
    promoted is the prompt to go and log it. ``upcoming_matches`` deliberately
    does not filter those out, so this flag is what separates "Saturday" from
    "you still haven't logged last Saturday".
    """

    sport = SportSerializer(read_only=True)
    position = SportPositionSerializer(read_only=True)
    is_overdue = serializers.SerializerMethodField()

    class Meta:
        model = MatchEntry
        fields = [
            "id",
            "date",
            "kickoff_time",
            "opponent_name",
            "match_type",
            "sport",
            "position",
            "is_overdue",
        ]
        read_only_fields = fields

    def get_is_overdue(self, obj):
        # Strictly before today: a fixture kicking off later this evening is
        # not overdue, whatever the clock says.
        return obj.date < timezone.localdate()


# =====================================================================
# WRITE
# =====================================================================

class MatchEntryStatWriteSerializer(serializers.Serializer):
    """
    One stat on the way in.

    ``source="stat_field"`` renames the key between the wire and the service:
    the client sends ``stat_field_id`` (which is what it is), and
    ``validated_data`` comes out keyed ``stat_field``, which is what
    ``MatchService._resolve_stats`` reads. One rename here beats a translation
    step in every view.

    Whether the value is allowed to have decimals is NOT decided here — it
    depends on the catalog row's ``value_type``, which is a second model, so the
    service owns it. ``max_digits``/``decimal_places`` mirror the column so an
    absurd number is a 400 rather than a numeric-overflow 500.
    """

    stat_field_id = serializers.UUIDField(source="stat_field")
    value = serializers.DecimalField(max_digits=8, decimal_places=2)


class MatchEntryCreateSerializer(serializers.Serializer):
    """
    POST body.

    ``sport`` and ``date`` are the only required fields — the whole design of
    the quick-add form is that a player can log a match in 30 seconds and fill
    the rest in later, or never.

    Nothing here checks that a scheduled match has no result, that the position
    belongs to the sport, or that a stat field is one of this sport's active
    ones. All three need a second model to answer and all three live in
    ``MatchService``.
    """

    sport = serializers.UUIDField()
    date = serializers.DateField()

    status = serializers.ChoiceField(
        choices=MatchEntry.Status.choices,
        required=False,
    )
    kickoff_time = serializers.TimeField(required=False, allow_null=True)
    opponent_name = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=MatchEntry._meta.get_field("opponent_name").max_length,
    )
    match_type = serializers.ChoiceField(
        choices=MatchEntry.MatchType.choices,
        required=False,
    )
    result = serializers.ChoiceField(
        choices=MatchEntry.Result.choices,
        required=False,
    )
    minutes_played = serializers.IntegerField(
        required=False,
        allow_null=True,
        min_value=0,
    )
    position = serializers.UUIDField(required=False, allow_null=True)
    self_rating = serializers.IntegerField(
        required=False,
        allow_null=True,
        min_value=1,
        max_value=5,
    )
    notes = serializers.CharField(required=False, allow_blank=True)
    career_entry = serializers.UUIDField(required=False, allow_null=True)

    photo_url = serializers.URLField(
        required=False,
        allow_blank=True,
        max_length=MatchEntry._meta.get_field("photo_url").max_length,
    )
    photo_public_id = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=MatchEntry._meta.get_field("photo_public_id").max_length,
    )

    stats = MatchEntryStatWriteSerializer(many=True, required=False)


class MatchEntryUpdateSerializer(serializers.Serializer):
    """
    PATCH body — send only what changes.

    Every field is optional and nothing has a default, because the service
    distinguishes "absent" from "sent as null" and a default would destroy that
    distinction. It matters most for ``stats``: sending the key replaces the
    whole set, leaving it out leaves the existing stats alone, and a
    ``default=[]`` here would silently wipe a player's stats on every PATCH that
    only meant to fix a typo in the notes.

    The promotion path — a fixture becoming a played match — is just this body
    carrying ``status``, ``result``, ``minutes_played``, ``self_rating`` and
    ``stats`` together. There is no separate endpoint for it, and no second row.
    """

    sport = serializers.UUIDField(required=False)
    date = serializers.DateField(required=False)

    status = serializers.ChoiceField(
        choices=MatchEntry.Status.choices,
        required=False,
    )
    kickoff_time = serializers.TimeField(required=False, allow_null=True)
    opponent_name = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=MatchEntry._meta.get_field("opponent_name").max_length,
    )
    match_type = serializers.ChoiceField(
        choices=MatchEntry.MatchType.choices,
        required=False,
    )
    result = serializers.ChoiceField(
        choices=MatchEntry.Result.choices,
        required=False,
    )
    minutes_played = serializers.IntegerField(
        required=False,
        allow_null=True,
        min_value=0,
    )
    position = serializers.UUIDField(required=False, allow_null=True)
    self_rating = serializers.IntegerField(
        required=False,
        allow_null=True,
        min_value=1,
        max_value=5,
    )
    notes = serializers.CharField(required=False, allow_blank=True)
    career_entry = serializers.UUIDField(required=False, allow_null=True)

    photo_url = serializers.URLField(
        required=False,
        allow_blank=True,
        max_length=MatchEntry._meta.get_field("photo_url").max_length,
    )
    photo_public_id = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=MatchEntry._meta.get_field("photo_public_id").max_length,
    )

    stats = MatchEntryStatWriteSerializer(many=True, required=False)

    def validate(self, attrs):
        if not attrs:
            raise serializers.ValidationError(
                "Nothing to update. Send at least one field."
            )
        return attrs
