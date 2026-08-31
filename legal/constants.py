"""
The version registry — the single source of truth for what a user must have
accepted before they are allowed to use the product.

A document is "current" at exactly one version at a time, and that version is a
DATE STRING, not a number, because it is the thing a lawyer and a screenshot
both agree on: "the terms as they stood on 2026-10-01". Nothing derives the
version from the file on the frontend, from a database row, or from a settings
value — it is written here, and bumping it here is what makes every user
pending again on their next request.

``requires_acceptance`` is the whole reason this is a dict rather than two
constants. Terms and privacy gate the product: a user who has not accepted the
current version is stopped. Guidelines and safety are published documents with
versions of their own — they appear in the same list, they are linked from the
same places, and they can still be recorded as accepted — but they never block
anybody. Adding a fifth document is an entry here plus, if it gates, a pair of
denormalized columns on ``accounts.User`` (see ``DENORMALIZED_FIELDS``).
"""

TERMS_VERSION = "2026-10-01"
PRIVACY_VERSION = "2026-10-01"

LEGAL_DOCUMENTS = {
    "terms":      {"version": TERMS_VERSION,   "requires_acceptance": True},
    "privacy":    {"version": PRIVACY_VERSION, "requires_acceptance": True},
    "guidelines": {"version": "2026-10-01",    "requires_acceptance": False},
    "safety":     {"version": "2026-10-01",    "requires_acceptance": False},
}

# Gating documents, in the order the consent screen presents them. Dict order is
# insertion order, so this follows LEGAL_DOCUMENTS above — which is what keeps
# the pending list deterministic instead of set-ordered.
REQUIRED_DOCUMENTS = tuple(
    key for key, document in LEGAL_DOCUMENTS.items()
    if document["requires_acceptance"]
)


def is_known_document(document) -> bool:
    return document in LEGAL_DOCUMENTS


def current_version(document) -> str:
    """The version of ``document`` a user must hold. Raises on an unknown key."""
    return LEGAL_DOCUMENTS[document]["version"]


def denormalized_fields(document):
    """
    The ``accounts.User`` columns that cache this document's acceptance, as
    ``(version_field, accepted_at_field)``.

    Derived from the key rather than mapped, so the registry stays the only
    list. The columns only exist for the gating documents — the caller checks
    with ``hasattr`` before writing, because a document that never blocks
    anybody has nothing to cache.
    """
    return f"{document}_version", f"{document}_accepted_at"
