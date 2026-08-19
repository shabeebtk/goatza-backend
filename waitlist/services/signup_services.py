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

  * MAIL NEVER FAILS THE REQUEST. The signup is the thing that matters; the
    notification is a convenience for me. Every failure path around it is
    swallowed and logged.
"""

import logging
import re

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Max

from utils.emails import send_email_async
from waitlist.models import PlayerSignup
from waitlist.selectors.signup_selectors import bust_signup_count, signup_count

logger = logging.getLogger(__name__)

# Everything that can precede a handle in something somebody pasted.
_INSTAGRAM_URL_PREFIX = re.compile(
    r"^(?:https?://)?(?:www\.)?instagram\.com/",
    re.IGNORECASE,
)


class PlayerSignupService:

    # Retries for the (max + 1) race. Five is far more than the contention this
    # ever sees — two people submitting inside the same millisecond — and it
    # bounds the loop so a genuinely broken unique constraint surfaces as an
    # error instead of spinning.
    MAX_NUMBER_ATTEMPTS = 5

    # Zero-padding for the public code: #413 becomes "GZ0413". Four digits
    # covers the pre-launch list comfortably; number 10000 simply produces
    # "GZ10000", which still fits ref_code's 12 characters.
    REF_CODE_PREFIX = "GZ"
    REF_CODE_DIGITS = 4

    # Default country code. This is a Kerala-first launch — a form that made
    # every player type "+91" would lose signups to a validation error.
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
        "district",
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

        Handles the shapes a Kerala player actually enters::

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
        """413 becomes "GZ0413" — the public, shareable form of the number."""
        return f"{cls.REF_CODE_PREFIX}{number:0{cls.REF_CODE_DIGITS}d}"

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
                        ref_code=cls.build_ref_code(next_number),
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
        """
        next_number = signup_count() + 1

        return {
            "signup_number": next_number,
            "ref_code": cls.build_ref_code(next_number),
            "name": str(data.get("name") or "").strip(),
            "district": data.get("district") or "",
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

            district = signup.get_district_display() or "No district"

            send_email_async(
                subject=f"New Goatza signup #{signup.signup_number} — {district}",
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
        """Every field, plain text, in the order I would read them."""
        instagram = f"@{signup.instagram}" if signup.instagram else "-"

        lines = [
            f"Signup #{signup.signup_number}  ({signup.ref_code})",
            "",
            f"Name            : {signup.name}",
            f"Phone           : {signup.phone}",
            f"Email           : {signup.email or '-'}",
            f"Instagram       : {instagram}",
            f"Date of birth   : {signup.date_of_birth or '-'}",
            f"District        : {signup.get_district_display() or '-'}",
            f"State           : {signup.state or '-'}",
            f"Sport           : {signup.sport or '-'}",
            f"Position        : {signup.get_position_display() or '-'}",
            f"Level           : {signup.get_level_display() or '-'}",
            f"Club / academy  : {signup.club_or_academy or '-'}",
            f"Source          : {signup.source or '-'}",
            f"Signed up at    : {signup.created_at}",
        ]

        return "\n".join(lines)
