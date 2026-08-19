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
"""

import datetime

from rest_framework import serializers

from waitlist.models import PlayerSignup

# Nobody signing up to be scouted is over this. It is a sanity bound on a typed
# year, not a product rule about who may play — the point is to catch "1902"
# and a mis-tapped date picker, not to turn anyone away.
MAX_AGE_YEARS = 60

MIN_PHONE_DIGITS = 10
MAX_PHONE_DIGITS = 15


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

    district = serializers.ChoiceField(
        choices=PlayerSignup.District.choices,
        required=False,
        allow_blank=True,
    )
    state = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=50,
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

    An allow-list of five harmless fields. ``phone``, ``email`` and
    ``instagram`` are the reason this is a hand-written field list on a public
    endpoint and not ``exclude``: a field added to the model later must not
    leak by default, it must be added here on purpose.
    """

    class Meta:
        model = PlayerSignup
        fields = [
            "name",
            "signup_number",
            "district",
            "position",
            "sport",
        ]
