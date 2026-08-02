"""
Request/response shaping for career entries.

Shape only. Every rule — ownership, the organization/verification coupling, the
sport ↔ position check, the date rules — lives in CareerEntryService, so the
same guarantees hold for any future internal caller (the recruitment pipeline
that auto-creates an entry on selection, for one).
"""

from rest_framework import serializers

from accounts.serializers.user_serializers import UserMiniSerializer
from careers.models import CareerEntry
from organization.serializers.organization_serializers import (
    OrganizationMiniSerializer,
)
from sports.serializers.sports_serializers import (
    SportPositionSerializer,
    SportSerializer,
)


class CareerEntrySerializer(serializers.ModelSerializer):
    """
    One entry as a profile renders it.

    ``organization`` is the nested club summary when the entry is linked to an
    org on Goatza, and null otherwise — ``organization_name`` is always present
    either way, which is what the card actually prints. Reading the name off the
    nested object would blank out entries whose club has since been deleted.
    """

    organization = OrganizationMiniSerializer(read_only=True)
    sport = SportSerializer(read_only=True)
    positions = SportPositionSerializer(many=True, read_only=True)

    class Meta:
        model = CareerEntry
        fields = [
            "id",
            "organization",
            "organization_name",
            "sport",
            "title",
            "entry_type",
            "squad_level",
            "age_group",
            "positions",
            "start_date",
            "end_date",
            "is_current",
            "description",
            "verification_status",
            "verified_at",
            "source",
            "created_at",
            "updated_at",
        ]


class CareerEntryCreateSerializer(serializers.Serializer):
    """
    POST body.

    ``verification_status`` is not accepted: it is derived from whether an
    organization was linked, and nobody verifies themselves.
    """

    organization = serializers.UUIDField(required=False, allow_null=True)
    organization_name = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=CareerEntry._meta.get_field("organization_name").max_length,
    )

    sport = serializers.UUIDField()
    title = serializers.CharField(
        max_length=CareerEntry._meta.get_field("title").max_length,
    )

    entry_type = serializers.ChoiceField(
        choices=CareerEntry.EntryType.choices,
        required=False,
    )
    squad_level = serializers.ChoiceField(
        choices=CareerEntry.SquadLevel.choices,
        required=False,
        allow_blank=True,
    )
    age_group = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=CareerEntry._meta.get_field("age_group").max_length,
    )

    positions = serializers.ListField(
        child=serializers.UUIDField(),
        required=False,
        allow_empty=True,
    )

    start_date = serializers.DateField()
    end_date = serializers.DateField(required=False, allow_null=True)
    is_current = serializers.BooleanField(required=False)

    description = serializers.CharField(required=False, allow_blank=True)


class CareerEntryUpdateSerializer(serializers.Serializer):
    """
    PATCH body — send only what changes.

    Every field is optional, and the service distinguishes "absent" from "sent
    as null": omitting ``organization`` leaves the link alone, sending null
    unlinks it. That is why nothing here has a default.
    """

    organization = serializers.UUIDField(required=False, allow_null=True)
    organization_name = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=CareerEntry._meta.get_field("organization_name").max_length,
    )

    sport = serializers.UUIDField(required=False)
    title = serializers.CharField(
        required=False,
        max_length=CareerEntry._meta.get_field("title").max_length,
    )

    entry_type = serializers.ChoiceField(
        choices=CareerEntry.EntryType.choices,
        required=False,
    )
    squad_level = serializers.ChoiceField(
        choices=CareerEntry.SquadLevel.choices,
        required=False,
        allow_blank=True,
    )
    age_group = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=CareerEntry._meta.get_field("age_group").max_length,
    )

    positions = serializers.ListField(
        child=serializers.UUIDField(),
        required=False,
        allow_empty=True,
    )

    start_date = serializers.DateField(required=False)
    end_date = serializers.DateField(required=False, allow_null=True)
    is_current = serializers.BooleanField(required=False)

    description = serializers.CharField(required=False, allow_blank=True)

    def validate(self, attrs):
        if not attrs:
            raise serializers.ValidationError(
                "Nothing to update. Send at least one field."
            )
        return attrs


# =====================================================================
# ORG VERIFICATION SIDE
# =====================================================================

class CareerClaimantSerializer(UserMiniSerializer):
    """
    Who is claiming the stint, as the review queue shows them. ``role`` is what
    tells a reviewer whether they are looking at a player, a coach or a scout —
    the same title means very different things across the three.
    """

    class Meta(UserMiniSerializer.Meta):
        fields = UserMiniSerializer.Meta.fields + ["role"]


class CareerVerificationRequestSerializer(serializers.ModelSerializer):
    """
    One row of an org's review queue.

    Deliberately not the same shape as CareerEntrySerializer: the reviewer
    already knows which org they are, so the nested organization is dropped and
    the claimant takes its place. Everything a club needs to recognise the
    person is here — who, what role, which sport and positions, which squad and
    age group, and over what dates.
    """

    user = CareerClaimantSerializer(read_only=True)
    sport = SportSerializer(read_only=True)
    positions = SportPositionSerializer(many=True, read_only=True)

    class Meta:
        model = CareerEntry
        fields = [
            "id",
            "user",
            "title",
            "sport",
            "positions",
            "entry_type",
            "squad_level",
            "age_group",
            "start_date",
            "end_date",
            "is_current",
            "description",
            "verification_status",
            "created_at",
        ]


class CareerFromApplicationSerializer(serializers.Serializer):
    """
    POST body for building an entry out of a selection — all optional.

    Only the three fields a player might reasonably want to say better than the
    prefill can. Organization, sport, positions, entry type and verification
    all come from the recruitment and are not client-settable: the whole point
    of this path is that the org's own pipeline is the source.
    """

    title = serializers.CharField(
        required=False,
        max_length=CareerEntry._meta.get_field("title").max_length,
    )
    start_date = serializers.DateField(required=False)
    description = serializers.CharField(required=False, allow_blank=True)


class CareerRejectSerializer(serializers.Serializer):
    """POST body for a rejection — an optional short note for the player."""

    reason = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=200,
    )
