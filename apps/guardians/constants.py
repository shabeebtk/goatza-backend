"""
The parental-consent reference data: the notice version, the two clocks, and
the values every consent row is allowed to hold.

WHY THE CHOICES LIVE HERE AND NOT IN models.py

They started in the model file, next to the columns that use them. They moved
because of who names them: the service writes them, the selectors filter on
them, the serializers and the admin render them, and the consent page will
eventually branch on them. That is four modules importing a model just to spell
a string — the same objection ``support/models.py`` raises against nesting
choices inside a model class, one step further out. ``models.py`` imports these;
nothing imports ``models.py`` to get them.

WHY THE NOTICE VERSION IS A DATE

Same reason as ``legal/constants.py``: it is the thing a parent's screenshot
and a lawyer's question both agree on — "the notice as it stood on
2026-10-01". Every consent event stores the version it was given, so the
version is what makes an approval evidence of something specific rather than
evidence that somebody clicked a button once.

BUMP ``GUARDIAN_NOTICE_VERSION`` WHENEVER THE PARENT-FACING WORDING CHANGES —
the email, the consent page, the list of what is processed. Unlike the legal
versions, a bump here does NOT re-open consent: an approval already given stays
valid and the child is not re-locked. It only means new approvals are recorded
against the new text, so that "which words was this parent shown" stays
answerable per row. Re-consenting everybody on a wording change is a product
decision nobody has made, and it is not made here by accident.
"""

import hashlib

from django.conf import settings
from django.db import models

GUARDIAN_NOTICE_VERSION = "2026-10-01"

# How long a consent LINK works, in days. Short on purpose: the link is a
# bearer credential — whoever holds it can approve a child — so it lives long
# enough for a parent to get to it after a weekend and no longer. A parent who
# misses the window is not stuck; the child resends, which mints a fresh token
# and kills the old one (see consent_service.resend).
TOKEN_TTL_DAYS = 7

# How long an unanswered REQUEST stays open before it is written off as
# expired, in days. A different clock from the one above and not a multiple of
# it: the token is about how long a secret may sit in an inbox, this is about
# how long we keep telling a child "your parent hasn't answered yet" before the
# honest answer becomes "nobody ever did". A resend restarts the link, not this.
#
# NOTHING SWEEPS ON THIS YET. The job that walks stale requests and writes the
# ``expired`` events is a later session; the number is here so that when it
# lands it reads this rather than inventing a second one.
GUARDIAN_CONSENT_EXPIRY_DAYS = 30

# The age at which a person may consent for a child, for the ONE narrow purpose
# of deciding whether a matched Goatza account is a plausible parent. A flat 18
# and deliberately NOT ``accounts.constants.minor_age_for`` — that table answers
# "may this person consent for THEMSELVES in their country", which is a lower
# bar in most of it (13 in the UK and the US). A 14-year-old in London is old
# enough to agree to their own account and nowhere near old enough to approve
# somebody else's child.
GUARDIAN_ADULT_AGE = 18

# Where the parent lands. A path rather than a whole setting, because
# ``FRONTEND_BASE_URL`` already exists and already owns the origin every
# transactional email points at — a second URL setting would be a second thing
# to get wrong on a deploy, and the two would disagree the first time only one
# was updated.
CONSENT_PAGE_PATH = "/guardian-consent"

# The query parameter carrying the raw token. Named once here because three
# places have to agree on it: this module builds the link, the consent view
# reads it back, and the frontend page pulls it out of the URL.
CONSENT_TOKEN_PARAM = "token"

# WHAT A PARENT IS AGREEING TO, itemized, in the words they are shown.
#
# ONE list, three renderings: the HTML email loops over it, the plain-text twin
# bullets it, and GET /guardian/consent/<token> returns it as an array. That is
# the entire reason it is here rather than written out in the template — a
# parent who approves from the page and a parent who approves from the email
# must be agreeing to the same sentence, and two copies of a list like this
# drift the first time somebody edits the one they happened to open.
#
# Plain sentences, no HTML entities: this text goes into JSON as readily as
# into a template, and an escaped "&mdash;" in an API response is a bug.
#
# CHANGING THIS LIST IS A NOTICE CHANGE. Bump GUARDIAN_NOTICE_VERSION with it,
# or the events written afterwards will claim a version whose wording nobody
# can reconstruct.
PROCESSED_DATA_ITEMS = (
    "Their name and date of birth",
    "Photos they upload — profile picture, cover, posts",
    "Posts and highlight videos they publish",
    "The city they choose to show",
    "Messages they send to other people on Goatza",
    "Their sport, position and playing history",
)


class GuardianConsentEventType(models.TextChoices):
    """
    What happened between us and a guardian, from our side of the exchange.

    The three ways a request ends are separate values rather than one "not
    approved", because they are not the same fact. DECLINED is a parent who
    said no, EXPIRED is a parent who never answered, and WITHDRAWN is a parent
    who said yes and later changed their mind. A child whose consent was
    withdrawn WAS consented for a period, and that period is something somebody
    may one day have to reconstruct.
    """

    REQUESTED = "requested", "Requested"
    RESENT = "resent", "Resent"
    APPROVED = "approved", "Approved"
    DECLINED = "declined", "Declined"
    WITHDRAWN = "withdrawn", "Withdrawn"
    EXPIRED = "expired", "Expired"


class GuardianConsentMethod(models.TextChoices):
    """
    HOW an approval reached us — the answer to "how do you know it was the
    parent and not the child".

    SHARED_CONTACT is the weak one, and it is kept as its own value precisely
    so it can be counted. It means the guardian's email or phone was the SAME
    one already on the child's account: the request went to an inbox the child
    can open, so the approval proves that somebody with access to that inbox
    agreed, and nothing beyond that. GOATZA_ACCOUNT (the contact matched a
    separate account that already existed) and SEPARATE_CONTACT (a channel the
    child does not control) are both stronger. Which of them the DPDP rules
    will accept is not decided yet, and collapsing the three into a plain
    "approved" now would throw away the only data that could answer it later.
    """

    GOATZA_ACCOUNT = "goatza_account", "Existing Goatza account"
    SEPARATE_CONTACT = "separate_contact", "Separate contact"
    SHARED_CONTACT = "shared_contact", "Shared contact"


class GuardianConsentLevel(models.TextChoices):
    """
    How hard the guardian's identity was checked.

    ACKNOWLEDGED is what the first release can honestly deliver: a parent
    followed a link sent to their own contact and confirmed. VERIFIED is
    reserved for whatever the DPDP rules end up demanding — a DigiLocker token,
    an ID check, a payment-instrument challenge — and the value exists NOW so
    that every row written before that arrives is labelled as the weaker thing.
    Adding the column later would mean every historical row defaulting into the
    stronger one, which is exactly the claim nobody can make on their behalf.
    """

    ACKNOWLEDGED = "acknowledged", "Acknowledged"
    VERIFIED = "verified", "Verified"


# The two event types that mean "a live link is out there". Both are answerable
# by a parent and neither is terminal; every path that resolves a token starts
# by asking whether the row it found is one of these.
OPEN_EVENT_TYPES = (
    GuardianConsentEventType.REQUESTED,
    GuardianConsentEventType.RESENT,
)

# The event types that END an exchange. A token whose newest row is one of
# these has been spent — see ``consent_service`` for why a declined link is
# just as spent as an approved one.
TERMINAL_EVENT_TYPES = (
    GuardianConsentEventType.APPROVED,
    GuardianConsentEventType.DECLINED,
    GuardianConsentEventType.WITHDRAWN,
    GuardianConsentEventType.EXPIRED,
)


def token_hash(raw_token) -> str:
    """
    SHA-256 hex of a raw consent token — 64 characters, exactly the width of
    ``GuardianConsentEvent.token_hash``.

    Plain SHA-256 and deliberately not a password hasher. The value being
    hashed is 256 bits of ``secrets`` output, not a human-chosen secret, so
    there is no dictionary to slow an attacker down with — and this runs on
    every click of every consent link, where a deliberately slow hash would buy
    nothing and cost the parent a spinner.

    Lives beside ``consent_page_url`` because the two are one idea: what goes
    in the link, and what gets written down instead of it. Both the service
    (which writes) and the selectors (which look up) call this, so neither owns
    it.
    """
    return hashlib.sha256(str(raw_token or "").encode("utf-8")).hexdigest()


def consent_page_url(raw_token) -> str:
    """
    The link a parent is sent, e.g.
    ``https://goatza.com/guardian-consent?token=<raw>``.

    THE RAW TOKEN GOES IN THE LINK AND NOWHERE ELSE. It is never stored, never
    logged and never returned to the child's device — only its SHA-256 hash is
    written down (``GuardianConsentEvent.token_hash``), so a copy of the
    database is not a pile of working "approve any child" credentials.

    Built from ``FRONTEND_BASE_URL`` for the same reason every other mailed
    link is: one origin, already stripped of its trailing slash, already
    overridable per environment.
    """
    base_url = (settings.FRONTEND_BASE_URL or "").rstrip("/")

    return f"{base_url}{CONSENT_PAGE_PATH}?{CONSENT_TOKEN_PARAM}={raw_token}"
