"""
The write path for parental consent.

Every function here does the same two things together: append one row to
``GuardianConsentEvent`` and move ``User.guardian_consent_status`` to whatever
that row means. Same reasoning as ``legal/services/acceptance_service.py`` —
a child whose status says approved while the events table holds no approval is
a child we cannot defend having unlocked, so the transaction is not an
optimisation, it is the point.

THE TOKEN, AND WHY THE RAW VALUE EXISTS FOR ABOUT A MILLISECOND

A parent has no account, so the link IS the credential: whoever opens it can
approve a child. The raw token therefore lives exactly as long as it takes to
build the email — it is generated, hashed, the hash is stored, the raw value
goes into one URL, and nothing keeps it. It is never returned to the child's
device, never logged, never written to a column. A dump of this database is a
pile of SHA-256 digests, not a pile of working "approve any child" links.

THE THREE WAYS A LINK STOPS WORKING, all enforced in ``_resolve_open_event``:

  * IT WAS ANSWERED. The answer row carries the same ``token_hash``, so the
    newest row for a hash IS the state of that exchange. A declined link is
    just as spent as an approved one — a parent who said no and then changed
    their mind does not get to re-answer the same request, the child sends a
    new one.
  * IT EXPIRED. ``TOKEN_TTL_DAYS`` after it was minted.
  * IT WAS SUPERSEDED. A resend mints a fresh token, and the old one dies the
    moment the new one exists — otherwise "resend" would mean "now there are
    two live credentials for this child", and every resend would widen the
    hole. The rule is that a link is live only if it is the newest open
    request FOR THAT CHILD.

WITHDRAWAL IS THE EXCEPTION TO ALL OF THAT. ``withdraw_by_token`` resolves an
APPROVED row and deliberately does not check expiry: the parent's ability to
take permission back must outlive the seven-day window they were given to grant
it, or "you can remove this later from the same link" — which is what the email
promises them — would be a lie after a week.

WHAT THIS MODULE DOES NOT DO: throttling (the view layer, next session), SMS
(there is no sender in this codebase yet — a phone-only guardian gets a
recorded request and no delivery, see ``request_consent``), and expiring stale
requests (``GUARDIAN_CONSENT_EXPIRY_DAYS`` is written down and nothing sweeps
on it yet).
"""

import logging
import secrets
from datetime import timedelta

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from accounts import constants as account_constants
from accounts.models import User
from guardians.constants import (
    GUARDIAN_ADULT_AGE,
    GUARDIAN_NOTICE_VERSION,
    OPEN_EVENT_TYPES,
    TOKEN_TTL_DAYS,
    GuardianConsentEventType,
    GuardianConsentLevel,
    GuardianConsentMethod,
    consent_page_url,
    token_hash,
)
from guardians.models import Guardian, GuardianConsentEvent
from utils.request_meta import client_ip, client_user_agent
from utils.transactional_emails import (
    send_guardian_consent_request_email,
    send_guardian_consent_withdrawn_email,
)
from utils.validations import is_valid_email, is_valid_phone

logger = logging.getLogger(__name__)

# 32 bytes of urandom, base64url — 43 characters. Long enough that guessing is
# not a strategy, short enough to survive an email client's line wrapping.
TOKEN_BYTES = 32

# Same truncation as legal/services/acceptance_service: a real browser can send
# more than the column holds, and a truncated user agent is context we still
# want. A DataError here is a consent a parent gave and we refused to store.
USER_AGENT_MAX_LENGTH = GuardianConsentEvent._meta.get_field("user_agent").max_length

INVALID_CONTACT_MESSAGE = (
    "Enter a valid email address or phone number for your parent or guardian."
)

PARENT_NAME_REQUIRED_MESSAGE = "Enter your parent or guardian's name."

# The four link failures a parent can hit, in the parent's words. They stay
# distinguishable by error_code — a client that cannot tell "expired" from
# "already answered" shows the wrong next step for both.
INVALID_LINK_MESSAGE = "This consent link isn't valid."

USED_LINK_MESSAGE = "This consent link has already been used."

EXPIRED_LINK_MESSAGE = (
    "This consent link has expired. Ask your child to send a new one."
)

SUPERSEDED_LINK_MESSAGE = (
    "A newer consent request was sent for this account. Please use the most "
    "recent email."
)

NOT_APPROVED_MESSAGE = (
    "There's no active permission on this link to remove."
)

NOT_REQUESTED_MESSAGE = (
    "No consent request has been started for this account yet."
)

# Said to a child who asks to approve on-device while their parent already has
# a real link waiting in their own inbox. See record_shared_contact_approval.
LINK_ALREADY_SENT_MESSAGE = (
    "We've sent a permission link to your parent or guardian. They need to "
    "approve it from their own email."
)


class GuardianConsentError(ValidationError):
    """A ValidationError carrying a stable machine code — see PhoneChangeError."""

    def __init__(self, message, code):
        super().__init__(message)
        self.error_code = code


# ---------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------

def _request_context(request):
    """
    ``(ip_address, user_agent)`` from a request, or ``(None, "")`` without one.

    ``request`` is optional throughout this module on purpose: a management
    command, a shell session and the expiry sweep all write real events, and
    none of them has an HTTP request to take context from.
    """
    if request is None:
        return None, ""

    return client_ip(request), client_user_agent(request)[:USER_AGENT_MAX_LENGTH]


def _normalize_contact(contact):
    """
    ``("email", "a@b.com")`` or ``("phone", "+919876543210")``, normalized to
    exactly what will be stored and compared.

    The email is lowercased in full — not just the domain. Storing
    ``Mum@Gmail.com`` and ``mum@gmail.com`` as two guardians would give one
    parent two rows, two links and two half-answered exchanges, and the
    find-or-create below matches on EXACT equality precisely so that it can be
    an index lookup rather than a scan.

    An "@" is what decides which of the two it is. That is cruder than parsing
    and it is right: a phone number never contains one, and anything else that
    does is an email address the user typed wrong, which they should be told
    about rather than have stored as a phone number.
    """
    if not contact or not isinstance(contact, str):
        raise GuardianConsentError(INVALID_CONTACT_MESSAGE, "invalid_contact")

    contact = contact.strip()

    if "@" in contact:
        contact = contact.lower()

        if not is_valid_email(contact):
            raise GuardianConsentError(INVALID_CONTACT_MESSAGE, "invalid_contact")

        return "email", contact

    if not is_valid_phone(contact):
        raise GuardianConsentError(INVALID_CONTACT_MESSAGE, "invalid_contact")

    return "phone", contact


def _clean_parent_name(parent_name):
    """The name as given, trimmed to the column. Blank is refused, never stored."""
    name = str(parent_name or "").strip()

    if not name:
        raise GuardianConsentError(
            PARENT_NAME_REQUIRED_MESSAGE, "parent_name_required"
        )

    return name[:Guardian._meta.get_field("name").max_length]


def _is_childs_own_contact(child, kind, value):
    """
    Whether this contact is one the CHILD already signs in with.

    The one check that decides which of the two flows runs, so it is
    deliberately generous: an approval sent to an address the child controls
    proves nothing about a parent, and the honest response is to say so
    (``shared_contact``) rather than to email the child a link to approve
    themselves.
    """
    if kind == "email":
        return bool(child.email) and child.email.strip().lower() == value

    return bool(child.phone) and child.phone.strip() == value


def _matching_user(kind, value):
    """
    The existing account this contact belongs to, or None.

    Case-insensitive on email because ``User.email`` is stored as the user typed
    it, and the guardian's copy has been lowercased by ``_normalize_contact`` —
    an exact match would miss every account created with a capital letter.
    """
    if kind == "email":
        return User.objects.filter(email__iexact=value).first()

    return User.objects.filter(phone=value).first()


def _find_or_create_guardian(kind, value, parent_name):
    """
    The guardian at this contact, creating one if this is the first child to
    name them.

    MATCHED ON THE CONTACT AND NEVER ON THE NAME. Two "Priya Nair" rows are two
    different parents until their email says otherwise, and merging them by
    name would attach one family's consent history to another family's child.
    The contact is the identity here because the contact is the only part of a
    guardian we can actually reach.

    An existing row KEEPS ITS NAME. The second child to name this parent may
    spell them differently ("Amma", "Mrs Nair"), and overwriting would rewrite
    what the first child's consent record says about who was asked.
    ``linked_user`` is filled in if it is still empty, since that is a fact
    about the contact rather than about either child's account.
    """
    lookup = {kind: value}

    # Oldest first: ids are UUIDv7, so this is the row the earliest sibling
    # created, and "the guardian" stays the same row on every later request
    # rather than moving whenever a duplicate slips in.
    guardian = Guardian.objects.filter(**lookup).order_by("id").first()

    if guardian is None:
        guardian = Guardian.objects.create(
            name=parent_name,
            email=value if kind == "email" else "",
            phone=value if kind == "phone" else "",
            linked_user=_matching_user(kind, value),
        )
        return guardian

    if guardian.linked_user_id is None:
        linked_user = _matching_user(kind, value)

        if linked_user is not None:
            guardian.linked_user = linked_user
            guardian.save(update_fields=["linked_user"])

    return guardian


def _mint_token():
    """``(raw_token, hashed, expires_at)``. The raw value is returned once."""
    raw_token = secrets.token_urlsafe(TOKEN_BYTES)

    return (
        raw_token,
        token_hash(raw_token),
        timezone.now() + timedelta(days=TOKEN_TTL_DAYS),
    )


def _set_status(child, status):
    """
    Move the denormalized cache on the child, and only if it actually moves.

    Called inside the caller's ``transaction.atomic()`` block every time, never
    on its own — the status and the event that justifies it are one write or
    neither.
    """
    if child.guardian_consent_status == status:
        return

    child.guardian_consent_status = status
    child.save(update_fields=["guardian_consent_status", "updated_at"])


def _newest_event_for_child(child):
    """The child's most recent event, or None. Rides ``guardian_child_recent_idx``."""
    return (
        GuardianConsentEvent.objects
        .select_related("guardian")
        .filter(child_id=child.pk)
        .first()
    )


def _resolve_open_event(raw_token):
    """
    The live request a parent's link points at, or a ``GuardianConsentError``
    saying precisely why there isn't one.

    See the module docstring for the three ways a link dies. The order of the
    checks is the order that gives the parent the most useful sentence: spent
    before expired (a link they already answered is not "expired"), expired
    before superseded (a month-old link is stale whether or not a newer one
    exists).
    """
    hashed = token_hash(raw_token)

    # Meta.ordering is -created_at, so this is the NEWEST row carrying the hash
    # — the answer row if the exchange was answered, the request otherwise.
    event = (
        GuardianConsentEvent.objects
        .select_related("guardian", "child")
        .filter(token_hash=hashed)
        .first()
    )

    if event is None:
        raise GuardianConsentError(INVALID_LINK_MESSAGE, "invalid_token")

    if event.event_type not in OPEN_EVENT_TYPES:
        raise GuardianConsentError(USED_LINK_MESSAGE, "token_already_used")

    if event.token_expires_at is None or event.token_expires_at <= timezone.now():
        raise GuardianConsentError(EXPIRED_LINK_MESSAGE, "token_expired")

    newest_open = (
        GuardianConsentEvent.objects
        .filter(child_id=event.child_id, event_type__in=OPEN_EVENT_TYPES)
        .first()
    )

    if newest_open is None or newest_open.token_hash != hashed:
        raise GuardianConsentError(SUPERSEDED_LINK_MESSAGE, "token_superseded")

    return event


def open_event_or_none(raw_token):
    """
    The live request behind ``raw_token``, or None — the read-only twin of
    ``_resolve_open_event``.

    EXISTS SO THERE IS ONE SET OF LIVENESS RULES, not two. The parent's consent
    page has to know whether a link still works before it renders an Approve
    button, and the alternative — the read layer re-deriving "spent, expired or
    superseded" for itself — is exactly how a link ends up rendered as live and
    then refused on submit. Same function, same three checks; this one answers
    None where the other raises.

    It deliberately does NOT say which of the three failed. The caller on the
    other side of this is an anonymous stranger holding a string, and telling
    them the difference between "never existed" and "expired last week" is
    telling them whether a token was ever real.
    """
    try:
        return _resolve_open_event(raw_token)
    except GuardianConsentError:
        return None


def _linked_user_is_adult(guardian):
    """
    Whether this guardian's matched Goatza account is old enough to be somebody's
    parent — the one thing that upgrades an approval to ``goatza_account``.

    False for every uncertainty: no linked account, no profile row, no birthdate
    on it. The upgrade is a claim that the approval came from a known adult
    account, and "we have no idea how old they are" does not support that claim.
    A flat 18 rather than the per-country table — see ``GUARDIAN_ADULT_AGE``.
    """
    linked_user = guardian.linked_user

    if linked_user is None:
        return False

    profile = getattr(linked_user, "profile", None)
    birthdate = getattr(profile, "birthdate", None)

    if birthdate is None:
        return False

    return account_constants.age_on(birthdate) >= GUARDIAN_ADULT_AGE


def _write_event(*, guardian, child, event_type, request=None, **fields):
    """
    Append one row. The only place this table is written, so every default that
    must hold on every row holds here.
    """
    ip_address, user_agent = _request_context(request)

    return GuardianConsentEvent.objects.create(
        guardian=guardian,
        child=child,
        event_type=event_type,
        notice_version=GUARDIAN_NOTICE_VERSION,
        ip_address=ip_address,
        user_agent=user_agent,
        **fields,
    )


def _given_name(parent_name):
    """A self-declared name, trimmed to the column. Blank is allowed here."""
    field = GuardianConsentEvent._meta.get_field("parent_name_given")

    return str(parent_name or "").strip()[:field.max_length]


# ---------------------------------------------------------------------
# Signup
# ---------------------------------------------------------------------

def ensure_pending_for_minor(user) -> bool:
    """
    Put a newly finished minor account into ``pending``, and say whether this
    account needs a guardian at all.

    Called from the two places a signup can COMPLETE — OTP verification on the
    email path, the role step on the Google path — because those are the two
    moments an account first has both halves of ``is_minor`` on file and a
    session in the user's hands. Returns True when the client should show the
    parent screen.

    ONLY ``not_needed`` MOVES. An approved or withdrawn account passing through
    again keeps what it has: this function's job is to set the opening state,
    not to overrule an answer a parent already gave. A minor who is already
    approved returns False — they need nothing — while a withdrawn one returns
    True, because a relocked account does need a guardian again.

    Adults are untouched, and the check is the cheap one: ``is_minor`` reads the
    birthdate off the profile and the jurisdiction off the user, both already
    loaded, and queries nothing.
    """
    if not user.is_minor:
        return False

    if user.guardian_consent_status == User.GuardianConsentStatus.NOT_NEEDED:
        _set_status(user, User.GuardianConsentStatus.PENDING)

        logger.info(f"ensure_pending_for_minor | locked pending | user={user.pk}")

    return user.guardian_consent_status != User.GuardianConsentStatus.APPROVED


# ---------------------------------------------------------------------
# Asking
# ---------------------------------------------------------------------

def request_consent(*, child, parent_name, parent_contact, request=None):
    """
    Ask a parent for permission, by whichever route the contact allows.

    Returns ``{"mode", "delivery", "guardian", "event"}``:

      * ``mode="shared_contact"`` — the contact given IS the child's own login
        email or phone. NOTHING IS SENT, because there is nothing to send it
        to: a link mailed to the child's own inbox is a link the child can
        click, and an approval collected that way is the child approving
        themselves. The caller's next step is the hand-the-phone flow
        (``record_shared_contact_approval``), which records exactly that and
        labels it as the weak method it is.

      * ``mode="separate_contact"`` — a channel the child does not control. A
        token is minted, its hash stored, and the link sent.

    ``delivery`` says what actually went out: ``"email"``, or ``"none"`` for a
    phone contact, because THIS CODEBASE HAS NO SMS SENDER. A phone-only
    guardian still gets a Guardian row and a recorded request — the evidence is
    correct — but nobody is reached, so the caller must tell the child to give
    an email address instead. That is a gap to close when a texting provider
    lands, not a silent failure to hide.

    A ``requested`` row is written in BOTH modes. It is what links this child to
    this guardian (there is nothing else to look them up by), it is what makes
    "we asked on the 3rd" true, and without it ``record_shared_contact_approval``
    would have no guardian to approve as.
    """
    parent_name = _clean_parent_name(parent_name)
    kind, value = _normalize_contact(parent_contact)

    shared = _is_childs_own_contact(child, kind, value)

    with transaction.atomic():
        guardian = _find_or_create_guardian(kind, value, parent_name)

        if shared:
            event = _write_event(
                guardian=guardian,
                child=child,
                event_type=GuardianConsentEventType.REQUESTED,
                request=request,
            )
            _set_status(child, User.GuardianConsentStatus.PENDING)

            logger.info(
                f"request_consent | shared contact, nothing sent | "
                f"child={child.pk} | guardian={guardian.pk}"
            )

            return {
                "mode": GuardianConsentMethod.SHARED_CONTACT,
                "delivery": "none",
                "guardian": guardian,
                "event": event,
            }

        raw_token, hashed, expires_at = _mint_token()

        event = _write_event(
            guardian=guardian,
            child=child,
            event_type=GuardianConsentEventType.REQUESTED,
            request=request,
            token_hash=hashed,
            token_expires_at=expires_at,
        )
        _set_status(child, User.GuardianConsentStatus.PENDING)

        delivery = _deliver_request(guardian, child, raw_token, kind)

    logger.info(
        f"request_consent | child={child.pk} | guardian={guardian.pk} | "
        f"delivery={delivery}"
    )

    return {
        "mode": GuardianConsentMethod.SEPARATE_CONTACT,
        "delivery": delivery,
        "guardian": guardian,
        "event": event,
    }


def _deliver_request(guardian, child, raw_token, kind):
    """
    Send the link, ON COMMIT, and say which channel carried it.

    ``transaction.on_commit`` rather than a plain call: the email contains a
    token that only works because a row says so, and a rollback after the send
    would put a live-looking link in a parent's inbox that resolves to nothing.
    Mailing a fraction of a second later costs nobody anything.
    """
    if kind != "email":
        logger.warning(
            f"request_consent | phone guardian, no SMS sender | "
            f"child={child.pk} | guardian={guardian.pk}"
        )
        return "none"

    consent_url = consent_page_url(raw_token)
    guardian_name = guardian.name
    child_username = child.username or str(child.pk)
    to_email = guardian.email

    transaction.on_commit(
        lambda: send_guardian_consent_request_email(
            guardian_name=guardian_name,
            email=to_email,
            child_username=child_username,
            consent_url=consent_url,
        )
    )

    return "email"


def resend(*, child, request=None):
    """
    Send the request again, on a NEW token. The previous link stops working.

    Returns the same dict shape as ``request_consent``.

    A fresh token rather than a re-send of the old one, and this is the whole
    reason resending is a service function instead of a second call to the
    mailer. Re-mailing the same token would leave two copies of one live
    credential in the world and would not help the case that actually needs a
    resend — a parent whose link has expired. Minting a new one gives them a
    full window and, because ``_resolve_open_event`` only accepts the newest
    open request for a child, retires the old link at the same instant.

    Throttling is the view's job (next session). Nothing here rate-limits, so
    calling this in a loop will happily mail a parent in a loop.
    """
    previous = _newest_event_for_child(child)

    if previous is None:
        raise GuardianConsentError(NOT_REQUESTED_MESSAGE, "consent_not_requested")

    guardian = previous.guardian

    # Which channel to use is decided by the guardian row, not by whatever the
    # last event happened to be: the parent's contact is what it is, and a
    # shared-contact request has no link to resend in the first place.
    kind = "email" if guardian.email else "phone"
    value = guardian.email or guardian.phone

    if _is_childs_own_contact(child, kind, value):
        raise GuardianConsentError(
            INVALID_CONTACT_MESSAGE, "shared_contact_not_resendable"
        )

    raw_token, hashed, expires_at = _mint_token()

    with transaction.atomic():
        event = _write_event(
            guardian=guardian,
            child=child,
            event_type=GuardianConsentEventType.RESENT,
            request=request,
            token_hash=hashed,
            token_expires_at=expires_at,
        )
        _set_status(child, User.GuardianConsentStatus.PENDING)

        delivery = _deliver_request(guardian, child, raw_token, kind)

    logger.info(
        f"resend | child={child.pk} | guardian={guardian.pk} | "
        f"delivery={delivery}"
    )

    return {
        "mode": GuardianConsentMethod.SEPARATE_CONTACT,
        "delivery": delivery,
        "guardian": guardian,
        "event": event,
    }


# ---------------------------------------------------------------------
# Answering
# ---------------------------------------------------------------------

def record_shared_contact_approval(
    *, child, parent_name, parent_birthdate=None, request=None
):
    """
    Record the hand-the-phone approval: the parent is standing next to the
    child, on the child's device, because the only contact they have is the
    child's own.

    Written down as ``shared_contact`` / ``acknowledged``, which is the honest
    label — nothing here proves a parent was present, and the value exists so
    that these approvals stay countable and separable when the DPDP rules say
    what is and is not enough. See ``GuardianConsentMethod``.

    IDEMPOTENT. A double-tapped button or a retried request must not append a
    second approval: if the child's newest event is already an approval, that
    row comes back untouched, keeping its original timestamp. Same rule, and
    the same reason, as ``legal.acceptance_service.record_acceptance``.

    REFUSED WHEN THE GUARDIAN IS REACHABLE SEPARATELY, and that guard is the
    reason this function checks anything at all. Without it a child could name
    a parent's real email, let the link go out, and then call this endpoint to
    approve themselves — the parent's inbox untouched, the row claiming an
    approval that never happened. So the standing request has to be one that
    genuinely had nowhere else to go: the guardian's contact must be the
    child's own. A child who wants this route after asking for a link says so
    the honest way, by starting a new request with their own contact.
    """
    newest = _newest_event_for_child(child)

    if newest is None:
        raise GuardianConsentError(NOT_REQUESTED_MESSAGE, "consent_not_requested")

    if newest.event_type != GuardianConsentEventType.APPROVED:
        guardian = newest.guardian
        kind = "email" if guardian.email else "phone"

        if not _is_childs_own_contact(child, kind, guardian.email or guardian.phone):
            logger.warning(
                f"record_shared_contact_approval | refused, guardian is "
                f"reachable separately | child={child.pk} | "
                f"guardian={guardian.pk}"
            )
            raise GuardianConsentError(
                LINK_ALREADY_SENT_MESSAGE, "consent_link_sent"
            )

    if newest.event_type == GuardianConsentEventType.APPROVED:
        logger.info(
            f"record_shared_contact_approval | already approved | "
            f"child={child.pk} | event={newest.pk}"
        )
        return newest

    with transaction.atomic():
        event = _write_event(
            guardian=newest.guardian,
            child=child,
            event_type=GuardianConsentEventType.APPROVED,
            request=request,
            method=GuardianConsentMethod.SHARED_CONTACT,
            level=GuardianConsentLevel.ACKNOWLEDGED,
            parent_name_given=_given_name(parent_name),
            parent_birthdate_given=parent_birthdate,
        )
        _set_status(child, User.GuardianConsentStatus.APPROVED)

    logger.info(
        f"record_shared_contact_approval | child={child.pk} | "
        f"guardian={newest.guardian_id}"
    )

    return event


def approve_by_token(*, raw_token, parent_name, parent_birthdate=None, request=None):
    """
    A parent said yes on their own link.

    ``separate_contact`` normally — the link travelled down a channel the child
    does not control, which is the whole strength of this route. It is upgraded
    to ``goatza_account`` when the guardian's contact matched an existing
    account AND that account's own birthdate puts them at 18 or over: an
    approval from a known adult account is a stronger record than one from an
    address we know nothing about, and the two must stay distinguishable in the
    table rather than being averaged into one "approved".

    The answer row carries the SAME ``token_hash`` as the request it answers.
    That is what makes the pair one exchange: the newest row for a hash is the
    state of that link, which is how a second click is refused and how
    ``withdraw_by_token`` finds an approval to revoke months later.
    """
    event = _resolve_open_event(raw_token)
    child = event.child

    method = (
        GuardianConsentMethod.GOATZA_ACCOUNT
        if _linked_user_is_adult(event.guardian)
        else GuardianConsentMethod.SEPARATE_CONTACT
    )

    with transaction.atomic():
        approval = _write_event(
            guardian=event.guardian,
            child=child,
            event_type=GuardianConsentEventType.APPROVED,
            request=request,
            method=method,
            level=GuardianConsentLevel.ACKNOWLEDGED,
            parent_name_given=_given_name(parent_name),
            parent_birthdate_given=parent_birthdate,
            token_hash=event.token_hash,
        )
        _set_status(child, User.GuardianConsentStatus.APPROVED)

    logger.info(
        f"approve_by_token | child={child.pk} | guardian={event.guardian_id} | "
        f"method={method}"
    )

    return approval


def decline_by_token(*, raw_token, request=None):
    """
    A parent said no.

    THE CHILD STAYS PENDING, and the status is deliberately not written at all.
    A decline is not a verdict on the account, it is a verdict on this request
    — very often it means the child typed the wrong address, or named the
    parent who was never going to be the one to answer. Locking them to a
    terminal state would leave a 15-year-old with a permanently dead account
    and no way back; leaving them pending means they can name a different
    guardian and try again, which is the behaviour the DPDP flow needs.

    The refusal itself is permanent evidence: this link is spent, this parent
    said no on this date, and the row says so forever.
    """
    event = _resolve_open_event(raw_token)

    with transaction.atomic():
        decline = _write_event(
            guardian=event.guardian,
            child=event.child,
            event_type=GuardianConsentEventType.DECLINED,
            request=request,
            token_hash=event.token_hash,
        )

    logger.info(
        f"decline_by_token | child={event.child_id} | "
        f"guardian={event.guardian_id} | child stays pending"
    )

    return decline


def withdraw_by_token(*, raw_token, request=None):
    """
    A parent took permission back, from the same link they granted it on.

    RELOCKS THE CHILD: status goes to ``withdrawn``, which is kept apart from
    ``pending`` because the two are not the same account. Pending has never been
    consented for and should be chased; withdrawn had consent and had it
    revoked, and chasing that parent with the same reminder would be harassing
    somebody who has already answered.

    NO EXPIRY CHECK, unlike every other path here — see the module docstring.
    The consent request expires; the right to revoke does not.

    Idempotent on a link that has already been withdrawn: the existing row comes
    back and nothing is written, so a parent who clicks twice does not produce
    two revocations of one approval.
    """
    hashed = token_hash(raw_token)

    event = (
        GuardianConsentEvent.objects
        .select_related("guardian", "child")
        .filter(token_hash=hashed)
        .first()
    )

    if event is None:
        raise GuardianConsentError(INVALID_LINK_MESSAGE, "invalid_token")

    if event.event_type == GuardianConsentEventType.WITHDRAWN:
        logger.info(
            f"withdraw_by_token | already withdrawn | child={event.child_id}"
        )
        return event

    if event.event_type != GuardianConsentEventType.APPROVED:
        raise GuardianConsentError(NOT_APPROVED_MESSAGE, "consent_not_approved")

    child = event.child
    guardian = event.guardian

    with transaction.atomic():
        withdrawal = _write_event(
            guardian=guardian,
            child=child,
            event_type=GuardianConsentEventType.WITHDRAWN,
            request=request,
            token_hash=hashed,
        )
        _set_status(child, User.GuardianConsentStatus.WITHDRAWN)

        if guardian.email:
            guardian_name = guardian.name
            to_email = guardian.email
            child_username = child.username or str(child.pk)

            transaction.on_commit(
                lambda: send_guardian_consent_withdrawn_email(
                    guardian_name=guardian_name,
                    email=to_email,
                    child_username=child_username,
                )
            )

    logger.info(
        f"withdraw_by_token | child={child.pk} | guardian={guardian.pk}"
    )

    return withdrawal
