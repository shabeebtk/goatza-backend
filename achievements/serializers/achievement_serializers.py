"""
Request/response shaping for achievements.

Shape only. Every rule — ownership, the caps, the issuer/verification coupling,
the career-entry ownership and sport checks, the date rules — lives in
AchievementService, so the same guarantees hold for any future internal caller.
"""

from rest_framework import serializers

from accounts.serializers.user_serializers import UserMiniSerializer
from achievements.models import Achievement
from careers.models import CareerEntry
from organization.serializers.organization_serializers import (
    OrganizationMiniSerializer,
)
from sports.serializers.sports_serializers import SportSerializer


class AchievementCareerEntryMiniSerializer(serializers.ModelSerializer):
    """
    The stint an award was won during, as the achievement card prints it —
    "Player at Kerala Blasters". Just enough to caption the link; the full entry
    is one tap away on the career section that already renders it.
    """

    class Meta:
        model = CareerEntry
        fields = [
            "id",
            "title",
            "organization_name",
        ]


class AchievementSerializer(serializers.ModelSerializer):
    """
    One achievement as a profile renders it.

    ``awarded_by`` is the nested issuer summary when the award is linked to an
    org on Goatza, and null otherwise — ``awarded_by_name`` is what the card
    actually prints, and is present either way. Reading the name off the nested
    object would blank out awards whose issuer has since been deleted, and would
    show nothing at all for the off-platform federations that only ever had the
    free-text name.
    """

    sport = SportSerializer(read_only=True)
    awarded_by = OrganizationMiniSerializer(read_only=True)
    career_entry = AchievementCareerEntryMiniSerializer(read_only=True)

    class Meta:
        model = Achievement
        fields = [
            "id",
            "title",
            "achievement_type",
            "sport",
            "description",
            "event_name",
            "level",
            "awarded_by",
            "awarded_by_name",
            "career_entry",
            "achieved_date",
            "image",
            "image_public_id",
            "reference_link",
            "is_pinned",
            "verification_status",
            "verified_at",
            "created_at",
        ]


class AchievementCreateSerializer(serializers.Serializer):
    """
    POST body.

    ``verification_status`` is not accepted: it is derived from whether an
    issuing organization was linked, and nobody verifies themselves.
    """

    title = serializers.CharField(
        max_length=Achievement._meta.get_field("title").max_length,
    )
    achievement_type = serializers.ChoiceField(
        choices=Achievement.AchievementType.choices,
        required=False,
    )

    sport = serializers.UUIDField()

    description = serializers.CharField(required=False, allow_blank=True)
    event_name = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=Achievement._meta.get_field("event_name").max_length,
    )
    level = serializers.ChoiceField(
        choices=Achievement.Level.choices,
        required=False,
        allow_blank=True,
    )

    awarded_by = serializers.UUIDField(required=False, allow_null=True)
    awarded_by_name = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=Achievement._meta.get_field("awarded_by_name").max_length,
    )

    career_entry = serializers.UUIDField(required=False, allow_null=True)

    achieved_date = serializers.DateField()

    image = serializers.URLField(
        required=False,
        allow_blank=True,
        max_length=Achievement._meta.get_field("image").max_length,
    )
    image_public_id = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=Achievement._meta.get_field("image_public_id").max_length,
    )

    reference_link = serializers.URLField(
        required=False,
        allow_blank=True,
        max_length=Achievement._meta.get_field("reference_link").max_length,
    )

    is_pinned = serializers.BooleanField(required=False)


class AchievementUpdateSerializer(serializers.Serializer):
    """
    PATCH body — send only what changes. Doubles as the pin/unpin call: there is
    no separate endpoint, ``{"is_pinned": true}`` is the whole request.

    Every field is optional, and the service distinguishes "absent" from "sent
    as null": omitting ``awarded_by`` leaves the link alone, sending null
    unlinks it. That is why nothing here has a default.
    """

    title = serializers.CharField(
        required=False,
        max_length=Achievement._meta.get_field("title").max_length,
    )
    achievement_type = serializers.ChoiceField(
        choices=Achievement.AchievementType.choices,
        required=False,
    )

    sport = serializers.UUIDField(required=False)

    description = serializers.CharField(required=False, allow_blank=True)
    event_name = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=Achievement._meta.get_field("event_name").max_length,
    )
    level = serializers.ChoiceField(
        choices=Achievement.Level.choices,
        required=False,
        allow_blank=True,
    )

    awarded_by = serializers.UUIDField(required=False, allow_null=True)
    awarded_by_name = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=Achievement._meta.get_field("awarded_by_name").max_length,
    )

    career_entry = serializers.UUIDField(required=False, allow_null=True)

    achieved_date = serializers.DateField(required=False)

    image = serializers.URLField(
        required=False,
        allow_blank=True,
        max_length=Achievement._meta.get_field("image").max_length,
    )
    image_public_id = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=Achievement._meta.get_field("image_public_id").max_length,
    )

    reference_link = serializers.URLField(
        required=False,
        allow_blank=True,
        max_length=Achievement._meta.get_field("reference_link").max_length,
    )

    is_pinned = serializers.BooleanField(required=False)

    def validate(self, attrs):
        if not attrs:
            raise serializers.ValidationError(
                "Nothing to update. Send at least one field."
            )
        return attrs


# =====================================================================
# ORG VERIFICATION SIDE
# =====================================================================

class AchievementClaimantSerializer(UserMiniSerializer):
    """
    Who is claiming the award, as the review queue shows them. ``role`` is what
    tells a reviewer whether they are looking at a player, a coach or a scout —
    the same trophy means very different things across the three.
    """

    class Meta(UserMiniSerializer.Meta):
        fields = UserMiniSerializer.Meta.fields + ["role"]


class AchievementVerificationRequestSerializer(serializers.ModelSerializer):
    """
    One row of an org's review queue.

    Deliberately not the same shape as AchievementSerializer: the reviewer
    already knows which org they are, so the nested issuer is dropped and the
    claimant takes its place. Everything needed to make the call is here — who,
    what role, what they say they won, at what level, at which event and when,
    plus the proof image and the reference link, which are the two things that
    actually settle a doubtful claim.

    ``is_pinned`` is absent on purpose: where the owner puts this on their own
    profile is not a reviewer's business and must not colour the decision.
    """

    user = AchievementClaimantSerializer(read_only=True)
    sport = SportSerializer(read_only=True)

    class Meta:
        model = Achievement
        fields = [
            "id",
            "user",
            "title",
            "achievement_type",
            "sport",
            "description",
            "event_name",
            "level",
            "achieved_date",
            "image",
            "reference_link",
            "verification_status",
            "created_at",
        ]


class AchievementRejectSerializer(serializers.Serializer):
    """POST body for a rejection — an optional short note for the owner."""

    reason = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=200,
    )
