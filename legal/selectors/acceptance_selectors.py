"""
Read side of legal consent.

``get_pending_documents`` is on the hot path — the consent gate asks it about
the current user, and a gate that costs a query per request is a gate nobody
will want to keep. So it reads the denormalized columns on the already-loaded
``User`` and touches no table at all. ``LegalAcceptance`` is the evidence trail;
it is not what this asks.
"""

from legal.constants import (
    LEGAL_DOCUMENTS,
    REQUIRED_DOCUMENTS,
    current_version,
    denormalized_fields,
)


def get_pending_documents(user) -> list[str]:
    """
    The documents ``user`` still has to accept, in presentation order.

    A document is pending when it gates the product and the version stored on
    the user is not the version the registry currently calls current. That
    covers both cases with one comparison:

      * never accepted   — the column is NULL, which equals no version
      * version bumped   — the column holds the superseded string

    Inequality rather than "older than", deliberately. Versions are dates, and
    date strings do sort, but the question is not "is this user behind" — it is
    "is this user holding the text we are currently serving". A rolled-back
    version must put everybody back in the gate, and a comparison that only
    looks backwards would let the newer acceptance stand.

    An empty list means the user is clear. An anonymous or unsaved user is
    everything-pending — the caller decides whether that is a login problem or
    a consent problem.
    """
    if not user or not getattr(user, "is_authenticated", False):
        return list(REQUIRED_DOCUMENTS)

    pending = []

    for document in REQUIRED_DOCUMENTS:
        version_field, _ = denormalized_fields(document)

        if getattr(user, version_field, None) != current_version(document):
            pending.append(document)

    return pending


def has_accepted(user, document) -> bool:
    """
    Whether ``user`` holds the current version of one document.

    The single-document form of ``get_pending_documents``, for a caller that
    already knows which one it cares about. A non-gating document has no stored
    version to compare, so it is never "not accepted" in the blocking sense.
    """
    return document not in get_pending_documents(user)


def legal_status(user) -> dict:
    """
    The ``legal`` block on GET user/details, and the one place its shape is
    decided.

    The client reads this on every session start to know whether to put the
    consent gate up, so it ships inside a payload the client already fetches
    rather than behind a request of its own::

        {"pending_documents": ["terms"], "requires_acceptance": true}

    ``requires_acceptance`` is redundant with a non-empty list and is there
    anyway: it is the question the client is actually asking, and a boolean
    cannot be got wrong the way ``.length > 0`` on a missing key can.
    """
    pending = get_pending_documents(user)

    return {
        "pending_documents": pending,
        "requires_acceptance": bool(pending),
        # What this user actually holds, per gating document — NULL where they
        # have never accepted. Settings prints it ("You accepted 2026-10-01"),
        # and it is the one thing no other endpoint can answer: legal/versions
        # says what is CURRENT, which is a different question and the wrong
        # answer to print next to a person's own record.
        "accepted_versions": {
            document: getattr(user, denormalized_fields(document)[0], None)
            for document in REQUIRED_DOCUMENTS
        },
    }


def current_versions() -> dict:
    """
    Every document and the version currently being served — the payload behind
    GET legal/versions.

    Includes the non-gating documents. A client that wants to badge "updated"
    next to the community guidelines needs their versions too, and splitting
    the response by whether a document blocks would just make the caller
    reassemble it.
    """
    return {
        document: values["version"]
        for document, values in LEGAL_DOCUMENTS.items()
    }
