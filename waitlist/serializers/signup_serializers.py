"""
Request/response shaping for the waitlist.

Shape only — normalisation and every rule that outlives one request live in
PlayerSignupService. What is enforced here is what makes a submission
*well-formed*: a name that is a name, a phone with a plausible number of
digits, a date of birth that could belong to a player.

Two things are deliberately NOT accepted on input:

  * ``notes`` — mine, written in the admin, never from outside.
  * ``source`` — read from the ``?src=`` query parameter by the view. It is an
    attribution tag the link carries, not something the form collects, and a
    body field would let anybody claim any campaign.

``signup_number`` and ``ref_code`` are assigned by the service; a client that
sends them is ignored (they are not fields here).

``location`` is the one field that is allowed to be wrong without being an
error. It is a geocoder result the browser attached, not something the player
typed, and no part of it is required — see ``validate_location``.
"""

import datetime

from rest_framework import serializers

from waitlist.models import PlayerSignup
from waitlist.selectors.signup_selectors import display_number, is_founding

# Nobody signing up to be scouted is over this. It is a sanity bound on a typed
# year, not a product rule about who may play — the point is to catch "1902"
# and a mis-tapped date picker, not to turn anyone away.
MAX_AGE_YEARS = 60

MIN_PHONE_DIGITS = 10
MAX_PHONE_DIGITS = 15

# The keys of a Mapbox result this app will pass on, and nothing else. Same
# allow-list reasoning as the form itself: LocationService reads a fixed set of
# keys, and anything outside it is either noise or somebody probing.
#
# ``name`` is the FULL label ("Kozhikode, Kerala, India") — it becomes
# Location.name and the signup's location_name. ``city`` is the short one. The
# frontend's MapboxCity calls those two ``label`` and ``name``, so ``label`` is
# accepted as an alias below for a client that forwards the object untouched.
LOCATION_FIELDS = (
    "name",
    "city",
    "state",
    "country",
    "country_code",
    "latitude",
    "longitude",
    "external_id",
)

MAX_LATITUDE = 90
MAX_LONGITUDE = 180


def _earliest_allowed_dob(today):
    """
    The oldest date of birth ``MAX_AGE_YEARS`` allows.

    Written out rather than ``today - timedelta(days=365.25 * 60)`` so the
    boundary is an actual birthday. February 29 has no counterpart 60 years
    back in a non-leap year, which is the one case ``replace`` raises on.
    """
    try:
        return today.replace(year=today.year - MAX_AGE_YEARS)
    except ValueError:
        return today.replace(year=today.year - MAX_AGE_YEARS, month=3, day=1)


def _coordinates(location):
    """
    ``(latitude, longitude)`` as floats, or ``(None, None)``.

    Both or neither: a point with one half of it is not a point, and storing
    half of one would put a signup on the equator or the prime meridian.
    """
    try:
        latitude = float(location["latitude"])
        longitude = float(location["longitude"])
    except (KeyError, TypeError, ValueError):
        return None, None

    if abs(latitude) > MAX_LATITUDE or abs(longitude) > MAX_LONGITUDE:
        return None, None

    return latitude, longitude


class PlayerSignupCreateSerializer(serializers.Serializer):
    """
    The join form.

    A plain Serializer, not a ModelSerializer: this is the public surface, and
    core/public_urls.py says the payload is an explicit allow-list. Listing the
    accepted fields by hand is what makes ``notes`` impossible to set by
    guessing at the model.
    """

    name = serializers.CharField(min_length=2, max_length=150, trim_whitespace=True)

    # Validated as digits below, not by a regex on the raw string: people write
    # numbers with spaces, dashes, brackets and a leading zero, and every one
    # of those is a number the service can normalise.
    phone = serializers.CharField(max_length=30)

    email = serializers.EmailField(required=False, allow_blank=True)

    # Accepts a handle, an @handle or a pasted profile URL — the service
    # reduces all three to the bare handle. 200 rather than the column's 100
    # because a URL with a tracking query string is longer than the handle it
    # contains, and it is the handle that has to fit.
    instagram = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=200,
    )

    date_of_birth = serializers.DateField(required=False, allow_null=True)

    # WHERE. Entirely optional, in every part: a player who dismisses the
    # location prompt still joins, and so does one whose browser sent something
    # the geocoder cannot make sense of.
    #
    # A DictField rather than a nested Serializer on purpose. A nested
    # serializer raises, and a raise here is a 400 — which would mean a bad
    # coordinate costing a signup. ``validate_location`` sanitises instead.
    location = serializers.DictField(required=False, allow_null=True)

    # Still accepted on its own, for a client with no geocoder at all. When a
    # location IS resolved, the service prefers the state that came with it.
    state = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=100,
    )

    sport = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=30,
    )
    position = serializers.ChoiceField(
        choices=PlayerSignup.Position.choices,
        required=False,
        allow_blank=True,
    )
    level = serializers.ChoiceField(
        choices=PlayerSignup.Level.choices,
        required=False,
        allow_blank=True,
    )
    club_or_academy = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=150,
    )

    # HONEYPOT.
    #
    # Rendered hidden and left empty by every human; filled in by the sort of
    # bot that submits every input it finds. Write-only and never persisted —
    # the view checks it and returns a normal-looking success without touching
    # the database (PlayerSignupService.decoy_payload).
    #
    # Named "website" rather than anything with "honeypot" or "trap" in it, for
    # the obvious reason.
    website = serializers.CharField(
        required=False,
        allow_blank=True,
        write_only=True,
        max_length=200,
    )

    def validate_name(self, value):
        # min_length runs before trimming, so " a " would pass it. Re-check.
        name = value.strip()

        if len(name) < 2:
            raise serializers.ValidationError(
                "Please enter your full name."
            )

        return name

    def validate_phone(self, value):
        digits = "".join(character for character in value if character.isdigit())

        if len(digits) < MIN_PHONE_DIGITS or len(digits) > MAX_PHONE_DIGITS:
            raise serializers.ValidationError(
                "Enter a valid phone number."
            )

        return value

    def validate_location(self, value):
        """
        Reduce a Mapbox result to the keys this app stores, dropping anything
        it cannot use — WITHOUT ever raising.

        Nothing in here is a 400. The location is optional, so every way it can
        be wrong degrades to a less precise signup rather than to a form the
        player has to fix:

            not an object / empty       ->  None  (no location at all)
            latitude outside [-90, 90]  ->  coordinates dropped, text kept
            longitude outside [-180,180]->  coordinates dropped, text kept
            unparseable numbers         ->  coordinates dropped, text kept

        Coordinates are dropped rather than the whole object because the text is
        still true and still useful: "Kozhikode" with no point on a map is what
        a signup from a client with no geocoder looks like anyway, and the
        service already handles exactly that shape (PlayerSignupService
        .resolve_location). What is lost is the Location FK, not the city.
        """
        if not value:
            return None

        if not isinstance(value, dict):
            return None

        location = {}

        for field in LOCATION_FIELDS:
            entry = value.get(field)
            if entry is None:
                continue
            location[field] = entry

        # MapboxCity's own naming: ``label`` is the full label, ``name`` is the
        # short city. Only used when the client sent the object as-is.
        label = value.get("label")
        if label and not value.get("city"):
            location["city"] = location.get("name") or ""
            location["name"] = label

        for field in ("name", "city", "state", "country", "country_code", "external_id"):
            if field in location:
                location[field] = str(location[field]).strip()

        latitude, longitude = _coordinates(location)

        if latitude is None or longitude is None:
            location.pop("latitude", None)
            location.pop("longitude", None)
        else:
            location["latitude"] = latitude
            location["longitude"] = longitude

        # Neither a label nor a city means there is nothing to store and
        # nothing the geocoder could match on either.
        if not location.get("name") and not location.get("city"):
            return None

        return location

    def validate_date_of_birth(self, value):
        if value is None:
            return value

        today = datetime.date.today()

        if value > today:
            raise serializers.ValidationError(
                "Date of birth cannot be in the future."
            )

        if value < _earliest_allowed_dob(today):
            raise serializers.ValidationError(
                "Please check your date of birth."
            )

        return value


class PlayerSignupCardSerializer(serializers.ModelSerializer):
    """
    The share card, by ref code — the ONLY shape this signup is ever public in.

    An allow-list of seven harmless fields. ``phone``, ``email`` and
    ``instagram`` are the reason this is a hand-written field list on a public
    endpoint and not ``exclude``: a field added to the model later must not
    leak by default, it must be added here on purpose.

    Three things the location block gives this endpoint are deliberately NOT
    here. ``latitude``/``longitude`` are a player's home city to five decimal
    places, published behind a four-digit code that appears in screenshots;
    ``location_name`` is the full label and often narrower than the city. The
    card shows ``city`` and ``country_code``, which is what a card needs to say
    where somebody is from.

    ``signup_number`` is the DISPLAY number — the raw column never leaves the
    server (waitlist.selectors.signup_selectors).
    """

    signup_number = serializers.SerializerMethodField()
    is_founding = serializers.SerializerMethodField()

    class Meta:
        model = PlayerSignup
        fields = [
            "name",
            "signup_number",
            "city",
            "country_code",
            "position",
            "sport",
            "is_founding",
        ]

    def get_signup_number(self, obj) -> int:
        return display_number(obj.signup_number)

    def get_is_founding(self, obj) -> bool:
        return is_founding(obj.signup_number)
