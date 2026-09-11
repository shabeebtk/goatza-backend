from rest_framework import serializers
from django.utils import timezone
from apps.accounts.models import UserProfile
from shared.models import Location
from apps.usernames.services.username_service import UsernameService
from utils.validations import validate_username_format

# How many times a user may change their own date of birth through the app.
# One, and the reason is in validate_birthdate below. Counted against
# User.birthdate_corrections, which the profile-update view increments when a
# change actually lands.
MAX_SELF_SERVE_BIRTHDATE_CHANGES = 1

BIRTHDATE_SUPPORT_MESSAGE = "Contact support to change your date of birth."


class UpdateUserProfileSerializer(serializers.Serializer):
    # User
    username = serializers.CharField(required=False, max_length=50)

    # Profile
    name = serializers.CharField(required=False)  # required but not blank
    headline = serializers.CharField(required=False, allow_blank=True)
    about = serializers.CharField(required=False, allow_blank=True)

    gender = serializers.ChoiceField(
        choices=UserProfile.Gender.choices,
        required=False,
        allow_blank=True
    )

    birthdate = serializers.DateField(required=False, allow_null=True)

    # THERE IS NO country_code FIELD HERE, AND THAT IS THE POINT.
    #
    # User.country_code is the legal jurisdiction the whole minor regime keys
    # off. Exposing it on the general profile editor would hand every user a
    # one-request switch from IN (consent age 18) to GB (13) — the same hole
    # age_service.resolve_country exists to close at signup, reopened
    # afterwards on a screen with no age check on it at all. It is set once,
    # at signup, cross-checked against the dialling code, and changed only by
    # support. UserProfile.country_code is a different column and IS written
    # from here, but only as a denormalized part of the location the user
    # picked — see the field comments on accounts/models.py.

    height_cm = serializers.IntegerField(required=False, allow_null=True)
    weight_kg = serializers.DecimalField(
        max_digits=5,
        decimal_places=2,
        required=False,
        allow_null=True
    )

    # location 
    location = serializers.DictField(required=False, allow_null=True)

    # VALIDATIONS

    def validate_username(self, value):
        """
        Format + availability against the SHARED namespace.

        Checking User alone was half the problem: it let a player take a handle
        an organization already held, and vice versa. This is still only a
        friendly pre-check — the arbiter is the unique constraint that
        UsernameService.claim writes against, so the view handles UsernameTaken
        as well.
        """
        user = self.context["request"].user

        try:
            available = UsernameService.is_available(value, exclude_user=user)
        except ValueError as e:
            # Malformed / reserved — a different answer from "taken", and the
            # message is the one the UI shows.
            raise serializers.ValidationError(str(e))

        if not available:
            raise serializers.ValidationError("Username already taken")

        # The NORMALIZED value, not the input: what gets claimed has to be what
        # was checked.
        return validate_username_format(value)
     

    def validate_name(self, value):
        if not value.strip():
            raise serializers.ValidationError("Name cannot be empty")
        return value

    def validate_birthdate(self, value):
        """
        One self-serve correction, then the support route.

        WHY THE LIMIT EXISTS

        The birthdate is collected at signup and decides, through
        ``User.is_minor``, which country's child-protection rules the account
        is held to. A freely editable birthdate makes that decision a
        preference: refused at 12, come back tomorrow as 20. Capping the
        self-serve path at one change is what turns the signup answer into an
        answer rather than a first guess.

        WHY THERE IS A SUPPORT ROUTE AT ALL, RATHER THAN A HARD LOCK

        Because people have a legal right to have inaccurate personal data
        about them corrected — GDPR Art. 16, and the DPDP Act's correction
        right for Indian users — and a birthdate is personal data like any
        other. A permanent lock would not be a strict version of this policy;
        it would remove a right the platform is obliged to provide, on the
        strength of a fat-fingered year. So the route stays open, it just
        stops being one click.

        Locking the SELF-SERVE path is what stops age-gaming. It is not meant
        to stop corrections, and it should not be tightened into something
        that does.
        """
        if value is None:
            # Clearing it is not a correction, it is a deletion — and it would
            # put the account back into the "age unknown" state that signup now
            # refuses to create. Nothing offers this in the UI; refuse it here
            # so nothing can.
            raise serializers.ValidationError(
                "Date of birth can't be removed. " + BIRTHDATE_SUPPORT_MESSAGE
            )

        if value > timezone.now().date():
            raise serializers.ValidationError("Birthdate cannot be in the future")

        user = self.context["request"].user
        profile = getattr(user, "profile", None)
        current = getattr(profile, "birthdate", None)

        # Re-submitting the same date is not a change and must not spend the
        # one correction. The profile editor PATCHes whatever fields the form
        # holds, so an unrelated edit — a new headline, a new city — routinely
        # carries the unchanged birthdate along with it.
        if current == value:
            return value

        if user.birthdate_corrections >= MAX_SELF_SERVE_BIRTHDATE_CHANGES:
            raise serializers.ValidationError(BIRTHDATE_SUPPORT_MESSAGE)

        return value

    def validate_height_cm(self, value):
        if value is not None and (value < 50 or value > 300):
            raise serializers.ValidationError("Height must be between 50 and 300 cm")
        return value

    def validate_weight_kg(self, value):
        if value is not None and (value < 20 or value > 300):
            raise serializers.ValidationError("Weight must be between 20 and 300 kg")
        return value

    def validate_location(self, value):
        """
        The place payload from docs/PLACES_MIGRATION.md 5.4.

        A DictField, so ``provider`` and ``external_id`` pass straight through
        to LocationService — they are the row's identity and dropping them here
        would mint a duplicate Location per save.

        Coordinates are no longer part of that identity: a payload carrying a
        place id is resolvable without them (the stored row already has a point,
        or the refresh job will fetch one). They are required only for a place
        nothing has ever seen, which LocationService enforces itself.
        """
        if value is None:
            return value

        if not isinstance(value, dict):
            raise serializers.ValidationError("Invalid location format")

        if not value.get("name"):
            raise serializers.ValidationError("name is required")

        # THE SERVER-SIDE HALF OF THE TOWN-ONLY RULE.
        #
        # The picker restricts the UI: it searches in `city` mode, which passes
        # settings.PLACES_CITY_PRIMARY_TYPES to Google and gets back localities,
        # taluks and panchayats — never a street address or a building. This
        # line is what makes that a rule rather than a suggestion, because a
        # crafted PATCH does not go through the picker and could otherwise put
        # a precise place_id on a profile.
        #
        # It applies to EVERY user, minor and adult alike. A sports profile
        # needs the town somebody plays in; it has never needed the doorstep,
        # and an adult's home address is not less theirs for being an adult's.
        #
        # Scoped to the PROFILE on purpose — posts and recruitments still name
        # real venues through `place` mode, because a recruitment that cannot
        # say which ground it is at is not a recruitment. See the note in
        # places/services/places_service.py.
        if value.get("type") != Location.Type.CITY:
            raise serializers.ValidationError("Please select a town or city.")

        has_coords = (
            value.get("latitude") is not None
            and value.get("longitude") is not None
        )

        if not has_coords and not value.get("external_id"):
            raise serializers.ValidationError(
                "latitude and longitude are required"
            )

        return value