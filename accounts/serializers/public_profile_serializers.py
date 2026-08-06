"""
What a logged-out visitor is allowed to see of a user profile.

Every field here is declared explicitly and NOTHING inherits from
``BaseUserSerializer``: that one carries ``email``, ``is_email_verified``,
``is_role_confirmed`` and ``is_onboarding_completed``, and anything built on top
of it inherits them for free (``UserFullSerializer`` does). A deny-list would
leak the next field somebody adds to the base; an allow-list cannot, and a test
pins the key set so a well-meaning addition here has to be deliberate.

Two omissions need justifying, because they are the two a recruiter would
reasonably ask for:

  * ``latitude`` / ``longitude`` — the profile carries the exact point the user
    picked. On a page anyone can scrape, that is a home or training-ground
    coordinate for a named individual, a large share of whom are minors.
    ``city`` + ``country_code`` carry all the recruiting signal ("is this player
    within travelling distance of our academy?") and none of the risk.

  * ``birthdate`` — full name plus exact date of birth is the classic
    identity-theft pair, and it is also the pair that makes a minor's public
    profile a safeguarding problem. ``age_group`` (U17, Senior …) is what a
    scout actually filters on, and it is derived here rather than sent raw.
"""

from datetime import date

from rest_framework import serializers

from accounts.models import User


def age_group_badge(birthdate):
    """
    "U17" / "Senior" / None — the same badge the client already renders from a
    raw birthdate in ``features/profile/utils/ageGroup.ts``.

    Mirrored server-side (rather than sending the birthdate and letting the
    client compute it) precisely so the birthdate never leaves the server on
    this path. The two implementations must agree: age <= 7 → U7, age <= 23 →
    U{age}, otherwise Senior.
    """
    if not birthdate:
        return None

    today = date.today()
    age = today.year - birthdate.year - (
        (today.month, today.day) < (birthdate.month, birthdate.day)
    )

    if age < 0:
        return None
    if age <= 7:
        return "U7"
    if age <= 23:
        return f"U{age}"
    return "Senior"


class PublicSportSerializer(serializers.Serializer):
    """One row of the sports rail, from a ``UserSport``."""

    name = serializers.CharField(source="sport.name")
    icon_name = serializers.CharField(source="sport.icon_name")
    icon_url = serializers.CharField(source="sport.icon_url")
    experience_level = serializers.CharField()
    is_primary = serializers.BooleanField()


class PublicPositionSerializer(serializers.Serializer):
    """One row from ``UserSportPosition`` — position within a sport."""

    name = serializers.CharField(source="position.name")
    sport = serializers.CharField(source="sport.name")
    is_primary = serializers.BooleanField()


# The attribute types that may appear on the public payload. Narrower than
# SportAttribute.DataType on purpose, and the reason is presentation rather
# than privacy: these values are rendered into a fixed-width slot on the
# generated share card. `text` is arbitrarily long free text and `multi_select`
# is comma soup — both destroy a layout sized for two words. A card is also the
# most-copied artifact we produce, so the narrow set is the safe default.
PUBLIC_ATTRIBUTE_DATA_TYPES = {"select", "boolean", "number"}


def public_sport_attributes(user, sport_id):
    """
    The player's own values for one sport's attributes, as
    ``[{"name", "data_type", "value"}]`` ordered by the attribute's
    ``display_order``.

    Sport-agnostic by construction: "Preferred foot" appears for a footballer
    and "Batting style" for a cricketer with no code here knowing either exists.

    Reads off the prefetched ``user.attributes`` list rather than filtering in
    SQL, so a profile bundle stays at a constant query count however many sports
    the player has logged.
    """
    rows = []

    for value in user.attributes.all():
        if value.sport_id != sport_id:
            continue

        attribute = value.attribute
        if attribute.data_type not in PUBLIC_ATTRIBUTE_DATA_TYPES:
            continue

        # `option` for a select, `value_text` for a number or boolean. A row
        # with neither cannot exist (there is a check constraint), but an empty
        # string can, and an empty slot is worse than a missing one.
        raw = value.option.value if value.option else value.value_text
        if not raw or not str(raw).strip():
            continue

        rows.append((
            attribute.display_order,
            attribute.name,
            {
                "name": attribute.name,
                "data_type": attribute.data_type,
                "value": str(raw).strip(),
            },
        ))

    # display_order is not unique within a sport, so name breaks the tie and the
    # order is stable — the card's default slot picks the first of these, and it
    # must not change between two renders of an unchanged profile.
    rows.sort(key=lambda row: (row[0], row[1]))
    return [row[2] for row in rows]


# The exact key set one attribute row may carry — same allow-list discipline as
# the profile itself, one level down.
PUBLIC_SPORT_ATTRIBUTE_KEYS = {"name", "data_type", "value"}


class PublicUserProfileSerializer(serializers.Serializer):
    """
    The header block of a public profile.

    A plain ``Serializer``, not a ``ModelSerializer``: ``fields`` on a
    ModelSerializer is a list of names against a model that keeps growing, and
    the whole point of this file is that adding a column to User or UserProfile
    can never widen the public payload by accident.

    Expects ``user.profile`` to be joined and ``sports__sport`` /
    ``positions__position`` / ``positions__sport`` /
    ``attributes__attribute`` / ``attributes__option`` prefetched.
    """

    id = serializers.UUIDField()
    username = serializers.CharField()
    role = serializers.CharField()
    created_at = serializers.DateTimeField()
    updated_at = serializers.SerializerMethodField()

    name = serializers.SerializerMethodField()
    headline = serializers.SerializerMethodField()
    about = serializers.SerializerMethodField()
    profile_photo = serializers.SerializerMethodField()
    cover_photo = serializers.SerializerMethodField()

    followers_count = serializers.SerializerMethodField()
    following_count = serializers.SerializerMethodField()
    connections_count = serializers.SerializerMethodField()

    height_cm = serializers.SerializerMethodField()
    weight_kg = serializers.SerializerMethodField()
    age_group = serializers.SerializerMethodField()

    location = serializers.SerializerMethodField()
    sports = serializers.SerializerMethodField()
    positions = serializers.SerializerMethodField()
    primary_sport = serializers.SerializerMethodField()

    # ── helpers ──────────────────────────────────────────────

    def _profile(self, obj):
        return getattr(obj, "profile", None)

    def get_updated_at(self, obj):
        """
        Cache-buster for the generated share card, which is cached for an hour
        at the CDN and must not survive a profile edit.

        The later of the two timestamps, because the card draws from both
        models: name, photos, height, weight and city live on ``UserProfile``,
        the username lives on ``User``, and a rename has to produce a fresh
        card just as a new photo does.

        Not sensitive on its own — any visible content change already implies
        it, and the payload carries the changed content anyway.

        Rendered through DateTimeField rather than returned raw, so it is the
        same ISO string ``created_at`` above produces: a method field hands back
        whatever Python object it is given, and a bare datetime would both read
        differently to the client and land in the bundle cache as a non-JSON
        value.
        """
        profile = self._profile(obj)
        stamp = obj.updated_at

        if profile is not None and profile.updated_at is not None:
            stamp = max(stamp, profile.updated_at)

        return serializers.DateTimeField().to_representation(stamp)

    def get_name(self, obj):
        profile = self._profile(obj)
        return profile.name if profile else ""

    def get_headline(self, obj):
        profile = self._profile(obj)
        return profile.headline if profile else ""

    def get_about(self, obj):
        profile = self._profile(obj)
        return profile.about if profile else ""

    def get_profile_photo(self, obj):
        profile = self._profile(obj)
        return profile.profile_photo if profile else ""

    def get_cover_photo(self, obj):
        profile = self._profile(obj)
        return profile.cover_photo if profile else ""

    def get_followers_count(self, obj):
        profile = self._profile(obj)
        return profile.followers_count if profile else 0

    def get_following_count(self, obj):
        profile = self._profile(obj)
        return profile.following_count if profile else 0

    def get_connections_count(self, obj):
        profile = self._profile(obj)
        return profile.connections_count if profile else 0

    def get_height_cm(self, obj):
        profile = self._profile(obj)
        return profile.height_cm if profile else None

    def get_weight_kg(self, obj):
        """
        Float, never Decimal. This payload is JSON-rendered here, but the same
        shape is reused by the DM share preview, which is msgpack'd onto the
        channel layer and can only carry primitives.
        """
        profile = self._profile(obj)
        if not profile or profile.weight_kg is None:
            return None
        return float(profile.weight_kg)

    def get_age_group(self, obj):
        profile = self._profile(obj)
        return age_group_badge(profile.birthdate) if profile else None

    def get_location(self, obj):
        """City-level only — see the module docstring for why."""
        profile = self._profile(obj)
        if not profile or not profile.city:
            return None

        return {
            "name": profile.location_name,
            "city": profile.city,
            "country_code": profile.country_code,
        }

    def get_sports(self, obj):
        return PublicSportSerializer(obj.sports.all(), many=True).data

    def get_positions(self, obj):
        return PublicPositionSerializer(obj.positions.all(), many=True).data

    def get_primary_sport(self, obj):
        """
        The one sport + position the header badges print, plus the player's
        values for that sport's own attributes. Read off the prefetched lists
        rather than re-filtering in SQL, so the bundle stays at a constant query
        count.

        ``attributes`` hangs off primary_sport rather than off the profile
        because that is what it is — attributes OF this sport. A player with two
        sports gets only their primary one's here, which is also all the share
        card can show.
        """
        primary = next(
            (s for s in obj.sports.all() if s.is_primary),
            None,
        )
        if primary is None:
            return None

        position = next(
            (
                p for p in obj.positions.all()
                if p.is_primary and p.sport_id == primary.sport_id
            ),
            None,
        )

        return {
            "sport": primary.sport.name,
            "icon_name": primary.sport.icon_name,
            "icon_url": primary.sport.icon_url,
            "experience_level": primary.experience_level,
            "primary_position": position.position.name if position else None,
            "attributes": public_sport_attributes(obj, primary.sport_id),
        }


# The exact key set the public user payload may contain. Imported by the test
# that fails the build if a private field ever appears — keep it in sync with
# the declared fields above and nowhere else.
PUBLIC_USER_PROFILE_KEYS = {
    "id",
    "username",
    "role",
    "created_at",
    "updated_at",
    "name",
    "headline",
    "about",
    "profile_photo",
    "cover_photo",
    "followers_count",
    "following_count",
    "connections_count",
    "height_cm",
    "weight_kg",
    "age_group",
    "location",
    "sports",
    "positions",
    "primary_sport",
}
