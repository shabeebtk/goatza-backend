"""
Write path for the pre-launch waitlist.

The view stays thin: it hands the validated body plus the ``?src=`` tag in and
every rule lives here — normalisation, the sequential number, the "you're
already in" case, and the notification mail.

Three rules are worth stating up front, because they are the ones that make
this app different from every other write path in the codebase:

  * A REPEAT PHONE IS NOT AN ERROR. There is no login here, so somebody who
    forgot whether they signed up has exactly one way to check: submit the form
    again. ``create`` returns the existing row and ``created=False`` instead of
    a 400, and it does NOT overwrite what is stored — the second submission is
    usually the same person typing less carefully, not a correction.

  * THE NUMBER IS THE PRODUCT. "You're #413" is what gets screenshotted, so
    ``signup_number`` is a dense sequence, assigned here inside a transaction
    as (max + 1). Deliberately not a Postgres sequence: sequences leave gaps on
    every rolled-back insert, and the local sqlite runs would not have one at
    all. The cost is a race between two concurrent inserts, which the unique
    constraint catches and the retry loop below resolves.

    The stored number is the honest one. The number the player is SHOWN is
    ``display_number(signup_number)`` — see ``build_ref_code`` for the one
    consequence of that which cannot be undone later.

  * MAIL NEVER FAILS THE REQUEST. The signup is the thing that matters; the
    notification is a convenience for me. Every failure path around it is
    swallowed and logged.

  * GEOCODING NEVER FAILS THE REQUEST EITHER. Location is optional at every
    level: the client may send none, the coordinates may be missing, and
    LocationService may raise or come back empty. Each of those saves the
    signup with whatever text was given and ``location=None``. A player lost
    because place search was slow is a player lost for nothing.
"""

import logging
import re

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Max

from services.location.location_service import LocationService
from utils.emails import send_email_async
from waitlist.models import PlayerSignup
from waitlist.selectors.signup_selectors import (
    bust_signup_count,
    display_count,
    display_number,
    founding_cutoff,
)

logger = logging.getLogger(__name__)

# Everything that can precede a handle in something somebody pasted.
_INSTAGRAM_URL_PREFIX = re.compile(
    r"^(?:https?://)?(?:www\.)?instagram\.com/",
    re.IGNORECASE,
)


def _as_float(value):
    """A coordinate as a float, or None. Never raises — see resolve_location."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class PlayerSignupService:

    # Retries for the (max + 1) race. Five is far more than the contention this
    # ever sees — two people submitting inside the same millisecond — and it
    # bounds the loop so a genuinely broken unique constraint surfaces as an
    # error instead of spinning.
    MAX_NUMBER_ATTEMPTS = 5

    # Zero-padding for the public code: display #413 becomes "GZ0413". Four
    # digits covers the pre-launch list comfortably; number 10000 simply
    # produces "GZ10000", which still fits ref_code's 12 characters.
    REF_CODE_PREFIX = "GZ"
    REF_CODE_DIGITS = 4

    # Default country code. India is where the list starts, so a form that made
    # every player there type "+91" would lose signups to a validation error.
    # It is only a fallback: anything written with a "+" or a "00" prefix keeps
    # the country code it came with, which is what makes the list work for a
    # player signing up from anywhere else.
    DEFAULT_COUNTRY_CODE = "91"

    # The columns ``create`` will accept. An allow-list, not **kwargs straight
    # into the model: ``notes`` is mine and ``signup_number``/``ref_code`` are
    # the server's, and none of the three may ever arrive from outside.
    ALLOWED_FIELDS = (
        "name",
        "phone",
        "email",
        "instagram",
        "date_of_birth",
        "state",
        "sport",
        "position",
        "level",
        "club_or_academy",
        "source",
    )

    # =================================================================
    # NORMALISATION
    # =================================================================

    @classmethod
    def normalise_phone(cls, raw) -> str:
        """
        Whatever somebody typed, turned into E.164.

        Handles the shapes a player in India actually enters, and leaves a
        number from anywhere else alone::

            9847012345        ->  +919847012345   (bare 10-digit mobile)
            098470 12345      ->  +919847012345   (trunk prefix, spaces)
            +91 98470-12345   ->  +919847012345   (already international)
            0091 9847012345   ->  +919847012345   (00 international prefix)
            00971501234567    ->  +971501234567   (an NRI player abroad)

        A leading "+" is always believed — if the caller went to the trouble of
        writing a country code, it is not this function's business to second-
        guess which one. Only a number with no country code at all gets +91.
        """
        value = str(raw or "").strip()
        if not value:
            return ""

        digits = re.sub(r"\D", "", value)
        if not digits:
            return ""

        # Explicit international forms, in the two ways they are written.
        if value.startswith("+"):
            return f"+{digits}"

        if digits.startswith("00"):
            return f"+{digits[2:]}"

        # National trunk prefix — a "0" in front of a domestic mobile number.
        digits = digits.lstrip("0")

        if len(digits) == 10:
            return f"+{cls.DEFAULT_COUNTRY_CODE}{digits}"

        # Already carries the country code, just without the plus.
        if digits.startswith(cls.DEFAULT_COUNTRY_CODE) and len(digits) > 10:
            return f"+{digits}"

        # Anything else: assume domestic. The serializer has already bounded
        # this to 10-15 digits, so it cannot overflow the column.
        return f"+{cls.DEFAULT_COUNTRY_CODE}{digits}"

    @classmethod
    def normalise_instagram(cls, raw) -> str:
        """
        Whatever somebody pasted, reduced to the bare handle, lowercased::

            @goatza                                ->  goatza
            https://www.instagram.com/goatza/      ->  goatza
            instagram.com/goatza?igsh=abc123       ->  goatza
            https://instagram.com/goatza/reel/xyz  ->  goatza

        Handles are case-insensitive on Instagram, so lowercasing loses nothing
        and makes the column searchable without a functional index.
        """
        value = str(raw or "").strip()
        if not value:
            return ""

        # Query string and fragment first — either can carry slashes and would
        # otherwise survive the path trimming below ("...?next=/foo").
        value = value.split("?", 1)[0]
        value = value.split("#", 1)[0]

        value = _INSTAGRAM_URL_PREFIX.sub("", value)
        value = value.strip("/")

        # A deep link into a post or reel leaves "goatza/reel/xyz" behind.
        value = value.split("/", 1)[0]

        value = value.lstrip("@").strip()

        max_length = PlayerSignup._meta.get_field("instagram").max_length
        return value.lower()[:max_length]

    @classmethod
    def build_ref_code(cls, number: int) -> str:
        """
        413 becomes "GZ0413" — the public, shareable form of the number.

        The number handed in is the DISPLAY number, not the stored one: the
        code appears next to "#37" on the card, and "GZ0001" beside "#37" is a
        card that looks broken.

        THE OFFSET MUST BE FIXED BEFORE GO-LIVE. Unlike the number, which is
        derived on every read and so follows the setting, ref_code is computed
        once and PERSISTED. Change ``WAITLIST_DISPLAY_OFFSET`` after real
        signups exist and every code written before the change keeps pointing
        at a number nobody will ever be shown again — and those codes are in
        screenshots, stories and the URLs of shared cards, where they cannot be
        corrected.
        """
        return f"{cls.REF_CODE_PREFIX}{number:0{cls.REF_CODE_DIGITS}d}"

    # =================================================================
    # LOCATION
    # =================================================================

    @classmethod
    def resolve_location(cls, raw) -> dict:
        """
        Turn the place-picker result the client sent into model fields.

        Returns the ``location`` FK plus the denormalised copy, ready to be
        merged into the create payload. The dict is always usable — there is no
        failure return, because there is no failure that is allowed to matter::

            no location sent        ->  {} (nothing written)
            coordinates missing     ->  text fields, location=None
            LocationService raises  ->  text fields, location=None
            LocationService resolves->  FK + the copy taken from the FK row

        The FK is resolved by the SHARED ``LocationService.get_or_create_location``
        — the same rows accounts and posts write against — so a place named on a
        signup and the same place named on a profile are one row, and converting
        a signup at launch does not create a duplicate.

        When the geocoder resolved, its values win over the raw payload: the
        Location row is deduplicated and edited centrally, and the point of
        having it is that it, not a stale client, is the authority on what the
        place is called.
        """
        if not raw or not isinstance(raw, dict):
            # The serializer already guarantees a dict or None; this is for the
            # management command or shell caller that does not go through it.
            return {}

        # The text copy, from the payload alone. This is what survives when the
        # FK cannot be resolved — a player who typed a city is still a player
        # in a city, and the admin can still work the list by it.
        fields = {
            "location": None,
            "location_name": str(raw.get("name") or "").strip()[:255],
            "city": str(raw.get("city") or "").strip()[:100],
            "country_code": str(raw.get("country_code") or "").strip().upper()[:5],
            "latitude": _as_float(raw.get("latitude")),
            "longitude": _as_float(raw.get("longitude")),
        }

        # ``state`` is a plain form field too, so it is only overwritten when
        # the location actually carries one — a client that geocodes without a
        # region must not blank out what the player typed.
        state = str(raw.get("state") or "").strip()[:100]
        if state:
            fields["state"] = state

        try:
            location = LocationService.get_or_create_location(raw)
        except Exception as e:
            # ValueError for missing or out-of-range coordinates, anything else
            # for a database problem underneath. Same answer either way.
            logger.warning(
                f"PlayerSignupService | Location not resolved | "
                f"name={fields['location_name'] or '-'} | {str(e)}"
            )
            return fields

        if location is None:
            # get_or_create_location's own race fallback can come back empty.
            logger.warning(
                f"PlayerSignupService | Location resolved to nothing | "
                f"name={fields['location_name'] or '-'}"
            )
            return fields

        denormalized = LocationService.build_denormalized(location)
        fields.update(denormalized)

        if location.state:
            fields["state"] = location.state[:100]

        return fields

    # =================================================================
    # CREATE
    # =================================================================

    @classmethod
    def create(cls, **data):
        """
        Put a player on the list.

        Returns ``(signup, created)``. ``created=False`` means the phone was
        already registered and the row is untouched — the caller should tell
        them they are already in and which number they are, not raise.

        ``location`` is read out of the payload separately from the allow-list:
        it arrives as the place object the client picked, not as columns, and
        ``resolve_location`` is what turns it into columns.
        """
        payload = {
            key: value
            for key, value in data.items()
            if key in cls.ALLOWED_FIELDS
        }

        payload["name"] = str(payload.get("name") or "").strip()
        payload["phone"] = cls.normalise_phone(payload.get("phone"))
        payload["instagram"] = cls.normalise_instagram(payload.get("instagram"))
        payload["email"] = str(payload.get("email") or "").strip().lower()

        # Resolved BEFORE the duplicate check, so the work is thrown away for a
        # repeat submission — but the alternative is resolving inside the retry
        # loop, where a slow geocoder would sit inside a transaction. A repeat
        # signup is rare; a transaction held open on a network call is not the
        # trade to make.
        payload.update(cls.resolve_location(data.get("location")))

        existing = (
            PlayerSignup.objects
            .filter(phone=payload["phone"])
            .first()
        )
        if existing is not None:
            logger.info(
                f"PlayerSignupService | Already registered | "
                f"signup_number={existing.signup_number}"
            )
            return existing, False

        signup = cls._create_with_number(payload)

        if signup is None:
            # Lost the phone race — somebody inserted this same number between
            # the check above and our INSERT. Same answer as if they had been
            # first, which is what the caller would have got a moment earlier.
            existing = (
                PlayerSignup.objects
                .filter(phone=payload["phone"])
                .first()
            )
            if existing is not None:
                return existing, False

            raise IntegrityError("Could not create the signup.")

        bust_signup_count()
        cls._notify(signup)

        logger.info(
            f"PlayerSignupService | Signup created | "
            f"signup_number={signup.signup_number} | ref_code={signup.ref_code}"
        )

        return signup, True

    @classmethod
    def _create_with_number(cls, payload):
        """
        Insert the row, assigning ``signup_number`` as (max + 1).

        The read and the insert share one transaction so the window is as small
        as the database can make it, but MAX() takes no lock — two concurrent
        callers can still read the same maximum, and the loser's INSERT trips
        the unique constraint. That is the whole point of the retry: it is a
        collision, not a failure, and the next attempt re-reads a maximum that
        now includes the winner's row.

        Returns None when the IntegrityError was the unique PHONE rather than
        the number, which means somebody registered this player while we were
        working; ``create`` resolves that case.
        """
        for attempt in range(1, cls.MAX_NUMBER_ATTEMPTS + 1):
            try:
                # Each attempt gets its own atomic block. An IntegrityError
                # marks a transaction unusable, so the retry has to happen
                # OUTSIDE the block that failed, never inside it.
                with transaction.atomic():
                    highest = (
                        PlayerSignup.objects
                        .aggregate(highest=Max("signup_number"))["highest"]
                        or 0
                    )
                    next_number = highest + 1

                    return PlayerSignup.objects.create(
                        signup_number=next_number,
                        # The CODE is built from the display number so it reads
                        # the same as the number printed beside it; the COLUMN
                        # keeps the honest one.
                        ref_code=cls.build_ref_code(display_number(next_number)),
                        **payload,
                    )

            except IntegrityError as e:
                # Was it the phone? Then retrying will never help.
                if PlayerSignup.objects.filter(phone=payload["phone"]).exists():
                    return None

                logger.warning(
                    f"PlayerSignupService | signup_number collision | "
                    f"attempt={attempt}/{cls.MAX_NUMBER_ATTEMPTS} | {str(e)}"
                )

                if attempt == cls.MAX_NUMBER_ATTEMPTS:
                    raise

    # =================================================================
    # HONEYPOT
    # =================================================================

    @classmethod
    def decoy_payload(cls, data) -> dict:
        """
        The success shape for a submission that tripped the honeypot.

        A bot that filled in the hidden ``website`` field gets a response
        indistinguishable from a real one — same keys, a plausible next number
        — and nothing is written. Telling it that it failed only teaches
        whoever wrote it to stop filling the field in.

        Lives next to ``create`` so the two shapes cannot drift apart: a decoy
        missing a key is a decoy that works exactly once.

        The number is the DISPLAY number, like every other number the API
        returns — a decoy that answered "#1" while the counter on the page said
        "#412" would label itself.
        """
        next_number = display_count() + 1

        return {
            "signup_number": next_number,
            "ref_code": cls.build_ref_code(next_number),
            "name": str(data.get("name") or "").strip(),
            "city": str((data.get("location") or {}).get("city") or "").strip(),
            "is_founding": next_number <= founding_cutoff(),
        }

    # =================================================================
    # NOTIFICATION
    # =================================================================

    @classmethod
    def _notify(cls, signup):
        """
        Tell me somebody signed up. Best effort, always.

        Wrapped whole rather than just around the send: ``send_email_async``
        only spawns a thread, so the network failure happens where nothing can
        catch it, but the thread spawn itself and the body formatting are still
        in the request. Neither may ever be the reason a player did not get on
        the list.
        """
        try:
            recipient = getattr(settings, "WAITLIST_NOTIFY_EMAIL", None)

            if not recipient:
                logger.info(
                    "PlayerSignupService | WAITLIST_NOTIFY_EMAIL is not set, "
                    "skipping the signup notification"
                )
                return

            place = signup.location_name or signup.city or "No location"

            send_email_async(
                subject=(
                    f"New Goatza signup "
                    f"#{display_number(signup.signup_number)} — {place}"
                ),
                message=cls._notification_body(signup),
                to_email=recipient,
            )

        except Exception as e:
            logger.error(
                f"PlayerSignupService | Notification failed | "
                f"signup_number={signup.signup_number} | {str(e)}"
            )

    @staticmethod
    def _notification_body(signup) -> str:
        """
        Every field, plain text, in the order I would read them.

        This is the ONE place both numbers appear together. The display number
        is what the player was told; the real one is the row in the database,
        and the two only line up again in the admin. Without both here, working
        out which row a player is talking about means doing the arithmetic in
        my head against a setting.
        """
        instagram = f"@{signup.instagram}" if signup.instagram else "-"

        if signup.latitude is not None and signup.longitude is not None:
            coordinates = f"{signup.latitude}, {signup.longitude}"
        else:
            coordinates = "-"

        lines = [
            f"Signup #{display_number(signup.signup_number)}  ({signup.ref_code})",
            f"Real number     : {signup.signup_number}",
            "",
            f"Name            : {signup.name}",
            f"Phone           : {signup.phone}",
            f"Email           : {signup.email or '-'}",
            f"Instagram       : {instagram}",
            f"Date of birth   : {signup.date_of_birth or '-'}",
            f"Location        : {signup.location_name or '-'}",
            f"Coordinates     : {coordinates}",
            f"City            : {signup.city or '-'}",
            f"State           : {signup.state or '-'}",
            f"Country         : {signup.country_code or '-'}",
            f"Sport           : {signup.sport or '-'}",
            f"Position        : {signup.get_position_display() or '-'}",
            f"Level           : {signup.get_level_display() or '-'}",
            f"Club / academy  : {signup.club_or_academy or '-'}",
            f"Source          : {signup.source or '-'}",
            f"Signed up at    : {signup.created_at}",
        ]

        return "\n".join(lines)
