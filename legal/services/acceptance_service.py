"""
The write path for legal consent.

One function, and it does three things that must happen together: write the
audit row, refresh the denormalized copy on the user, and do neither if the
other fails. A user whose ``terms_version`` says they accepted while the audit
table holds no row is a user we cannot defend having let through — so the
transaction is not an optimisation here, it is the point.

Two rules worth stating up front:

  * RE-RECORDING IS NOT AN ERROR. A double-tapped consent button, a retried
    request, a user who accepts on two devices — all of it lands on the same
    (user, document, version) and must produce exactly one row. ``get_or_create``
    against the unique constraint is what makes that true, and the row it finds
    keeps its ORIGINAL ``accepted_at``. The denormalized timestamp is copied
    from that row, never from ``now()``, so the cache and the evidence agree on
    when the user actually agreed.

  * THE VERSION IS NEVER PASSED IN. Callers name documents; the version comes
    from ``LEGAL_DOCUMENTS``. A client that could send its own version could
    accept a version that does not exist, or re-accept a superseded one and
    quietly downgrade the denormalized columns back into pending.
"""

import logging

from django.db import transaction

from legal.constants import (
    LEGAL_DOCUMENTS,
    current_version,
    denormalized_fields,
    is_known_document,
)
from legal.models import LegalAcceptance

logger = logging.getLogger(__name__)

# The column is 500 wide and a real browser can send more than that (extension
# soup, some in-app webviews). A truncated user agent is context we still want;
# a DataError is a consent the user gave and we refused to store.
USER_AGENT_MAX_LENGTH = LegalAcceptance._meta.get_field("user_agent").max_length


def record_acceptance(*, user, documents, ip_address=None, user_agent=""):
    """
    Record that ``user`` accepted each document in ``documents``, at whatever
    version the registry currently says is current.

    Returns the list of ``LegalAcceptance`` rows for those documents — existing
    ones where the acceptance was already on file, new ones otherwise.

    Raises ``ValueError`` on the first unknown document key, BEFORE anything is
    written: a payload naming one real document and one typo records neither,
    because a half-applied consent is worse than a rejected one.

    Documents that do not gate the product ("guidelines", "safety") are accepted
    here and get their audit row like any other — they simply have no columns on
    ``User`` to refresh, so they never affect what ``get_pending_documents``
    returns.
    """
    if not user or not user.pk:
        raise ValueError("A saved user is required to record an acceptance")

    # Deduplicated, order preserved. Two "terms" in one payload is one
    # acceptance, and the caller should get one row back, not the same row
    # twice.
    documents = list(dict.fromkeys(documents or []))

    if not documents:
        raise ValueError("At least one document is required")

    for document in documents:
        if not is_known_document(document):
            raise ValueError(f"Unknown legal document: {document!r}")

    user_agent = str(user_agent or "").strip()[:USER_AGENT_MAX_LENGTH]

    acceptances = []
    changed_fields = []

    with transaction.atomic():
        for document in documents:
            version = current_version(document)

            acceptance, created = LegalAcceptance.objects.get_or_create(
                user=user,
                document=document,
                version=version,
                defaults={
                    "ip_address": ip_address,
                    "user_agent": user_agent,
                },
            )
            acceptances.append(acceptance)

            if not created:
                # Already on file. The stored ip/user agent belong to the
                # original acceptance and are deliberately left alone — this
                # request is not new evidence, it is the same evidence again.
                logger.info(
                    f"record_acceptance | Already recorded | user={user.pk} | "
                    f"document={document} | version={version}"
                )

            changed_fields.extend(
                _refresh_denormalized(user, document, acceptance)
            )

        if changed_fields:
            user.save(update_fields=changed_fields + ["updated_at"])

    logger.info(
        f"record_acceptance | user={user.pk} | "
        f"documents={','.join(documents)}"
    )

    return acceptances


def _refresh_denormalized(user, document, acceptance):
    """
    Copy this acceptance onto the user's cached columns, and return the names of
    the columns that actually changed (so ``update_fields`` stays minimal).

    Silently writes nothing for a document with no columns — that is the normal
    case for a non-gating document, not a fault. A gating document with no
    columns IS a fault, and it surfaces loudly rather than leaving a user stuck
    pending forever against a column that does not exist.
    """
    version_field, accepted_at_field = denormalized_fields(document)

    if not hasattr(user, version_field):
        if LEGAL_DOCUMENTS[document]["requires_acceptance"]:
            raise AttributeError(
                f"{document!r} gates the product but accounts.User has no "
                f"{version_field!r} column — add the pair before requiring it"
            )
        return []

    changed = []

    if getattr(user, version_field) != acceptance.version:
        setattr(user, version_field, acceptance.version)
        changed.append(version_field)

    if getattr(user, accepted_at_field) != acceptance.accepted_at:
        setattr(user, accepted_at_field, acceptance.accepted_at)
        changed.append(accepted_at_field)

    return changed
