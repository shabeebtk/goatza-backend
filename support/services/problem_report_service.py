"""
"Report a problem" — the write side, shared by both endpoints.

ONE service for two views. The authenticated route and the anonymous one differ
in what they may send (screenshots) and in what they must send (a contact
email), and both of those are enforced in the serializers, which is the layer
that knows which route it is. Everything that is true of a problem report
whoever filed it lives here.

Three rules are worth stating up front:

  * ``reported_by`` IS ALWAYS THE HUMAN. A bug is experienced by a person, and
    any reply goes to a person — a club has no inbox. When the caller is acting
    as an org, ``acting_org`` records that for context and ``reported_by``
    still points at the logged-in user. Note that ``core.actor.Actor`` carries
    ``user=None`` for an org actor, so the VIEW has to pass ``request.user``
    separately; reading ``actor.user`` here would silently anonymise every
    report filed from a club account.

  * ``client_context`` IS NEVER STORED AS SENT. It arrives from a client, and
    on the public route from an unauthenticated one, and it lands in a
    JSONField. An unbounded blob on an anonymous POST is a free write of
    arbitrary size into the database. It is reduced to an allow-list of keys,
    each coerced to a string and truncated — see ``sanitise_context``.

  * A LINK-HEAVY REPORT IS FLAGGED, NEVER REJECTED. Three URLs is what spam
    looks like, but it is also what a real report looks like when somebody
    pastes the three pages that break. It saves with ``SPAM_SUSPECT`` and sits
    in a filtered queue: a false positive that a human can still find beats one
    that vanished at the door.
"""

import logging
import re

from django.db import IntegrityError, transaction

from services.storage.validators import (
    allowed_image_extensions,
    validate_media,
)
from support.models import ProblemReport, ProblemCategory, ProblemStatus
from support.services.reference import generate_reference

logger = logging.getLogger(__name__)


# Anything a person types into a "what went wrong" box. The floor exists
# because "broken" is not a report anybody can act on; the ceiling because the
# column is unbounded text on a route an anonymous caller can reach.
MAX_DESCRIPTION_LENGTH = 2000
MIN_DESCRIPTION_LENGTH = 15

# Matches the POLICY entry in accounts/views/user_upload_signature_views.py.
# Re-checked here because the signing endpoint and the create endpoint are two
# separate requests — nothing forces a client to have asked for only three.
MAX_SCREENSHOTS = 3

# The ONLY keys kept from client_context, and the width of each value. Anything
# else the client sends is dropped without comment: this is diagnostic context,
# not a place for the client to define its own schema.
ALLOWED_CONTEXT_KEYS = (
    "path",
    "app_version",
    "viewport",
    "timezone",
    "platform",
    "network",
    "actor_type",
)
MAX_CONTEXT_VALUE_LENGTH = 200

# Links in the description. Two is a normal report ("this page and this one");
# the third is the spam signal — see the module docstring for why it flags
# rather than refuses.
MAX_LINKS = 2
_URL_PATTERN = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)

# ``user_agent`` is CharField(500) and the header is caller-controlled, so it
# is cut here rather than trusted to be short.
MAX_USER_AGENT_LENGTH = 500

# Re-rolls of the reference when a concurrent insert takes the code between
# the generate and the INSERT. Five is far more than the contention this ever
# sees (32^6 codes), and it bounds the loop so a genuinely broken unique
# constraint surfaces as an error instead of spinning.
MAX_REFERENCE_ATTEMPTS = 5


class ProblemReportService:

    MAX_DESCRIPTION_LENGTH = MAX_DESCRIPTION_LENGTH
    MIN_DESCRIPTION_LENGTH = MIN_DESCRIPTION_LENGTH
    MAX_SCREENSHOTS = MAX_SCREENSHOTS

    # =================================================================
    # CREATE
    # =================================================================

    @classmethod
    def create(
        cls,
        *,
        category,
        description,
        actor=None,
        user=None,
        contact_email="",
        screenshots=None,
        client_context=None,
        ip_address=None,
        user_agent="",
    ):
        """
        File one problem report.

        ``actor`` is ``request.actor`` (None on the public route) and decides
        only ``acting_org``. ``user`` is ``request.user`` and is what gets
        stored as the reporter — see the module docstring.

        Returns ``(True, {"reference": "GZ-7K4M2P"})``, or ``(False, message)``
        for something the reporter can act on. The success payload carries the
        reference and NOTHING else: no id, no status, no echo of what was sent.
        A confirmation is not a receipt.
        """
        description = (description or "").strip()

        error = cls._validate(category, description)
        if error:
            return False, error

        success, screenshot_urls = cls._resolve_screenshots(actor, user, screenshots)
        if not success:
            return False, screenshot_urls

        report = cls._insert(
            category=category,
            description=description[:MAX_DESCRIPTION_LENGTH],
            user=user,
            acting_org=actor.organization if (actor and actor.is_org) else None,
            contact_email=(contact_email or "").strip(),
            screenshots=screenshot_urls,
            client_context=cls.sanitise_context(client_context),
            ip_address=ip_address,
            user_agent=(user_agent or "")[:MAX_USER_AGENT_LENGTH],
            status=cls.initial_status(description),
        )

        logger.info(
            "ProblemReportService | Report filed | reference=%s | category=%s | "
            "status=%s | authenticated=%s",
            report.reference,
            report.category,
            report.status,
            bool(user and user.is_authenticated),
        )

        return True, {"reference": report.reference}

    # =================================================================
    # VALIDATION
    # =================================================================

    @staticmethod
    def _validate(category, description):
        """The rules both routes share. Returns a message, or None."""
        if category not in ProblemCategory.values:
            return "Choose what kind of problem this is"

        if len(description) < MIN_DESCRIPTION_LENGTH:
            return (
                f"Tell us a little more — at least "
                f"{MIN_DESCRIPTION_LENGTH} characters"
            )

        if len(description) > MAX_DESCRIPTION_LENGTH:
            return (
                f"Description is too long (max "
                f"{MAX_DESCRIPTION_LENGTH} characters)"
            )

        return None

    @classmethod
    def _resolve_screenshots(cls, actor, user, screenshots):
        """
        Validate the attached screenshots and return the URLs to store.

        Returns ``(True, [url, ...])`` or ``(False, message)``.

        Each entry is ``{"url", "key"}`` and every one is re-checked against
        the CALLER's own storage prefix. The upload endpoint signed those PUTs
        into ``users/<id>/support`` or ``organizations/<id>/support``, but the
        create request is a separate call and a client can send any string it
        likes — without this check a report could reference somebody else's
        object and pull a private image into our admin.

        Anonymous callers may not attach anything. A presigned PUT handed to a
        logged-out caller is a write path into the bucket from the open
        internet; logged-out reports are text-only, deliberately.

        What is STORED is the URL alone. The key exists to prove ownership at
        submit time, and the admin renders a plain list of URLs.
        """
        entries = screenshots or []

        if not entries:
            return True, []

        if not user or not user.is_authenticated:
            return False, "Sign in to attach screenshots"

        if len(entries) > MAX_SCREENSHOTS:
            return False, f"You can attach up to {MAX_SCREENSHOTS} screenshots"

        org = actor.organization if (actor and actor.is_org) else None

        urls = []

        for entry in entries:
            url = str((entry or {}).get("url") or "").strip()
            key = str((entry or {}).get("key") or "").strip()

            if not url or not key:
                return False, "Invalid screenshot"

            try:
                validate_media(
                    user,
                    url,
                    key,
                    org=org,
                    allowed_extensions=allowed_image_extensions(),
                )
            except ValueError as e:
                logger.warning(
                    "ProblemReportService | Screenshot rejected | %s", str(e)
                )
                return False, "Invalid screenshot"

            urls.append(url)

        return True, urls

    # =================================================================
    # SANITISING
    # =================================================================

    @staticmethod
    def sanitise_context(client_context):
        """
        ``client_context`` reduced to the keys we read, as short strings.

        An allow-list, not a blocklist, and every value coerced with ``str``
        rather than trusted: a nested object or a list would otherwise be
        stored whole, and the size of what a caller can write is the entire
        point of the limit. Unknown keys are dropped silently — a client
        sending something new is not an error, it just does not get stored
        until this tuple names it.
        """
        if not isinstance(client_context, dict):
            return {}

        context = {}

        for key in ALLOWED_CONTEXT_KEYS:
            value = client_context.get(key)

            if value is None:
                continue

            text = str(value).strip()[:MAX_CONTEXT_VALUE_LENGTH]

            if text:
                context[key] = text

        return context

    @staticmethod
    def initial_status(description):
        """
        ``SPAM_SUSPECT`` for a link-heavy description, otherwise ``NEW``.

        Counted on the description as typed. The report is still SAVED — see
        the module docstring — it just lands in a queue a human filters rather
        than in the one they work through.
        """
        if len(_URL_PATTERN.findall(description or "")) > MAX_LINKS:
            return ProblemStatus.SPAM_SUSPECT

        return ProblemStatus.NEW

    # =================================================================
    # INSERT
    # =================================================================

    @classmethod
    def _insert(cls, *, user, **fields):
        """
        Insert with a fresh reference, retrying the code on a collision.

        The unique constraint is the arbiter — we do NOT generate, check
        whether it is taken and then insert, because the gap between the SELECT
        and the INSERT is exactly the race two concurrent reports would win.
        The IntegrityError IS the "taken" answer. Same reasoning as
        ``UsernameService.claim``.
        """
        reporter = user if (user and user.is_authenticated) else None

        for attempt in range(1, MAX_REFERENCE_ATTEMPTS + 1):
            try:
                # Each attempt gets its own atomic block. An IntegrityError
                # marks a transaction unusable, so the retry has to happen
                # OUTSIDE the block that failed, never inside it — same shape
                # as PlayerSignupService._create_with_number.
                with transaction.atomic():
                    return ProblemReport.objects.create(
                        reference=generate_reference(),
                        reported_by=reporter,
                        **fields,
                    )

            except IntegrityError as e:
                logger.warning(
                    "ProblemReportService | reference collision | "
                    "attempt=%s/%s | %s",
                    attempt,
                    MAX_REFERENCE_ATTEMPTS,
                    str(e),
                )

                if attempt == MAX_REFERENCE_ATTEMPTS:
                    raise

    # =================================================================
    # HONEYPOT
    # =================================================================

    @classmethod
    def decoy_payload(cls):
        """
        The success shape for a submission that tripped the honeypot.

        A bot that filled in the hidden ``website`` field gets a response
        indistinguishable from a real one — same key, a real-looking code — and
        nothing is written. Telling it that it failed only teaches whoever
        wrote it to stop filling the field in.

        Lives next to ``create`` so the two shapes cannot drift apart: a decoy
        missing a key is a decoy that works exactly once. The code is generated
        by the same function real references come from, so it is not
        distinguishable by shape either — it simply matches no row.
        """
        return {"reference": generate_reference()}
