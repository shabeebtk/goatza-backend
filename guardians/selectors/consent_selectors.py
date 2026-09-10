"""
Read side of parental consent.

THE GATE MUST NOT COME THROUGH HERE. Whatever eventually blocks a minor's
photos, DMs or discoverability reads ``user.guardian_consent_status`` directly —
one field on a user the request has already loaded, no query — exactly as the
legal gate reads ``terms_version``. That is what the denormalized column is
for, and a gate that costs a join is a gate somebody will later be tempted to
cache badly.

What lives here is the other kind of read: the settings screen and the consent
page, which need the story around the status — who was asked, when, has the
link expired — and can afford one indexed query to get it.
"""

from accounts.models import User
# mask_email lives with the deletion flow because that is where it was first
# needed; email_change_service imports it across apps for the same reason.
# One masking rule for the whole product beats three that degrade
# differently on a two-letter local part.
from accounts.services.account_deletion_service import mask_email
from guardians.constants import (
    GUARDIAN_NOTICE_VERSION,
    OPEN_EVENT_TYPES,
    PROCESSED_DATA_ITEMS,
    GuardianConsentEventType,
    token_hash,
)
from guardians.models import GuardianConsentEvent
from guardians.services import consent_service


def get_status(child) -> dict:
    """
    The consent block for ``child``: the status plus enough context to render a
    screen about it.

    ``status`` is read from the denormalized column and costs nothing. The rest
    comes from ONE query for the newest event — the read
    ``guardian_child_recent_idx`` exists to serve — and is None-heavy on
    purpose: a child who has never named a guardian has a status and no story,
    and that is a normal state rather than a missing row to paper over.

    ``guardian_name`` is what the CHILD said their parent was called, not what
    the parent typed when they answered. This is the child's own screen, and
    the name they recognise is the one they entered.
    """
    status = getattr(
        child,
        "guardian_consent_status",
        User.GuardianConsentStatus.NOT_NEEDED,
    )

    newest = (
        GuardianConsentEvent.objects
        .select_related("guardian")
        .filter(child_id=child.pk)
        .first()
    )

    if newest is None:
        return {
            "status": status,
            "guardian_name": None,
            "guardian_contact": None,
            "last_event": None,
            "last_event_at": None,
            "link_expires_at": None,
        }

    guardian = newest.guardian

    return {
        "status": status,
        "guardian_name": guardian.name,
        # The channel the request went down, so the child can see WHERE to go
        # looking for it — "check mum's email" is the single most useful thing
        # this screen can say to somebody whose parent has not answered.
        "guardian_contact": guardian.email or guardian.phone,
        "last_event": newest.event_type,
        "last_event_at": newest.created_at,
        # Only meaningful while a link is actually out there. An answered or
        # superseded request carries a deadline that has stopped mattering, and
        # showing it would start a countdown to nothing.
        "link_expires_at": (
            newest.token_expires_at
            if newest.event_type in (
                GuardianConsentEventType.REQUESTED,
                GuardianConsentEventType.RESENT,
            )
            else None
        ),
    }



def mask_contact(contact) -> str:
    """
    ``priya@gmail.com`` -> ``p***a@gmail.com``; ``+919876543210`` -> ``+91••••3210``.

    Enough for a child to recognise WHICH of their parents' inboxes to go
    nagging, not enough to be read off their screen by somebody else on the
    bus. The email half is the product's existing ``mask_email`` rather than a
    second rule of its own.

    The phone half keeps the dialling code and the last four digits, which is
    the shape every OTP screen in the country already trains people to read.
    """
    if not contact:
        return ""

    if "@" in contact:
        return mask_email(contact)

    tail = contact[-4:]
    head = contact[:-4]

    # A number too short to have a head is returned as stars plus its tail
    # rather than in full — a degraded mask, never a leaked one.
    return f"{head[:3]}{'•' * max(len(head) - 3, 0)}{tail}" if head else f"••••{tail}"


def guardian_status(child) -> dict:
    """
    The ``guardian`` block on GET /user/details, and the one place its shape is
    decided::

        {"status": "pending", "masked_contact": "p***a@gmail.com"}

    Rides along on the call the client already makes at session start, exactly
    as ``legal_status`` does, because a locked child's waiting screen needs to
    render on the same round trip that tells it the account is locked.

    ONE QUERY, AND ONLY WHEN PENDING. Every other status — the adults, the
    approved minors, the withdrawn ones — is answered from the denormalized
    column with no query at all, which is the whole reason that column exists.
    A waiting screen is the only state that needs to name a contact.

    The contact comes from the newest OPEN request rather than the newest event
    of any kind: after a decline the child is still pending, and the address
    worth showing them is the one a link actually went to.
    """
    status = getattr(
        child,
        "guardian_consent_status",
        User.GuardianConsentStatus.NOT_NEEDED,
    )

    if status != User.GuardianConsentStatus.PENDING:
        return {"status": status, "masked_contact": None}

    newest_request = (
        GuardianConsentEvent.objects
        .select_related("guardian")
        .filter(child_id=child.pk, event_type__in=OPEN_EVENT_TYPES)
        .first()
    )

    if newest_request is None:
        # Pending and nobody named yet — a minor who has just finished signup
        # and has not reached the parent form. The client shows that form.
        return {"status": status, "masked_contact": None}

    guardian = newest_request.guardian

    return {
        "status": status,
        "masked_contact": mask_contact(guardian.email or guardian.phone),
    }


def get_event_by_token(raw_token):
    """
    The newest event carrying this token's hash, or None.

    A PLAIN LOOKUP AND NOTHING MORE. It does not check expiry, does not check
    whether the link was already answered and does not check whether a newer
    request superseded it — ``consent_service._resolve_open_event`` owns all
    three, and a second implementation of those rules living in the read layer
    is exactly how a link ends up accepted by one path and refused by the other.

    Use it to RENDER: the consent page needs the child's username and the
    guardian's name before the parent has decided anything, and it needs them
    for expired and answered links too, so that it can say what happened
    instead of showing a blank page. Use the service to ACT.
    """
    return (
        GuardianConsentEvent.objects
        .select_related("guardian", "child")
        .filter(token_hash=token_hash(raw_token))
        .first()
    )



# The two states a resolved token can be shown in. Everything else — unknown,
# expired, superseded, declined, already withdrawn — is not a state on this
# surface at all: consent_page returns None and the view answers one generic
# refusal. See its docstring.
STATE_PENDING = "pending"
STATE_APPROVED = "approved"


def consent_page(raw_token) -> dict | None:
    """
    Everything the parent's page renders, or **None when there is nothing a
    stranger may be shown**.

    THE None IS THE SECURITY BOUNDARY, and it is why this returns a whole
    payload rather than the event. The caller is anonymous and the token is the
    entire proof of identity, so the rule is enforced here, once: either the
    token resolves to a live request or a standing approval — in which case the
    holder has already demonstrated they are the parent it was sent to — or the
    view has literally nothing in hand to leak. A view that received an event
    and decided for itself what to render is a view one refactor away from
    putting a child's username in a 404.

    Liveness is not decided here. ``consent_service.open_event_or_none`` owns
    it, so this page and the approve endpoint can never disagree about whether
    a link still works.

    ``notice_version`` differs by state on purpose. A pending page shows the
    CURRENT version, because that is the text this parent would be agreeing to
    if they press Approve now; an approved page shows the version stored on the
    approval, because that is the text they actually did agree to, whatever has
    been published since.
    """
    event = consent_service.open_event_or_none(raw_token)
    state = STATE_PENDING

    if event is None:
        newest = get_event_by_token(raw_token)

        # A standing approval, and nothing else. A declined or withdrawn
        # exchange is over — the child has to start a new one — and a spent
        # link must not keep rendering a page about somebody's child.
        if newest is None or newest.event_type != GuardianConsentEventType.APPROVED:
            return None

        event = newest
        state = STATE_APPROVED

    child = event.child

    return {
        "state": state,
        # USERNAME, never the profile name. The whole surface identifies the
        # child by handle — the email does too — because this page is reachable
        # by anyone holding the link, including whoever received it by typo.
        "child_username": child.username or "",
        "processed_data": list(PROCESSED_DATA_ITEMS),
        "notice_version": (
            GUARDIAN_NOTICE_VERSION
            if state == STATE_PENDING
            else event.notice_version
        ),
        "siblings": [
            sibling.username or ""
            for sibling in guardian_other_approved_children(
                event.guardian, exclude_child=child
            )
        ],
        # Only while a link is live. On an approved page the deadline has
        # stopped meaning anything, and a countdown to nothing is worse than
        # no countdown.
        "expires_at": event.token_expires_at if state == STATE_PENDING else None,
    }


def guardian_other_approved_children(guardian, exclude_child=None):
    """
    The other children this guardian has ALREADY approved — the sibling hint.

    What it is for: a parent who approved one child last season should not be
    put through the same explanation for the second, and the child's screen can
    say "your parent has already approved <sibling>" instead of pretending this
    is a first contact.

    Two conditions, and both are needed. The events table says this guardian
    approved that child ONCE; ``guardian_consent_status`` says whether the
    approval still stands. Reading only the events would hand the hint a child
    whose consent was withdrawn last week, which is precisely the family this
    must not get wrong.

    Returns a queryset of ``User``, distinct — a child with two approvals in
    their history (approved, withdrawn, approved again) is still one sibling.
    """
    queryset = (
        User.objects
        .filter(
            guardian_consent_events__guardian=guardian,
            guardian_consent_events__event_type=GuardianConsentEventType.APPROVED,
            guardian_consent_status=User.GuardianConsentStatus.APPROVED,
        )
        .distinct()
    )

    if exclude_child is not None:
        queryset = queryset.exclude(pk=exclude_child.pk)

    return queryset
