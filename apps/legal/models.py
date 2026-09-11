from django.conf import settings
from django.db import models

from shared.models import BaseUUIDModel


class LegalAcceptance(BaseUUIDModel):
    """
    One row per (user, document, version) — the evidence that a specific person
    agreed to a specific text at a specific moment.

    THIS TABLE IS APPEND-ONLY. Nothing updates a row and nothing deletes one.
    That is not a style preference: the table's entire job is to be the answer
    when somebody asks, months later, whether a user accepted the terms and
    which terms those were. A row that can be edited answers nothing. The
    version bump is what supersedes an old acceptance — the old row stays.

    ``accounts.User`` carries a denormalized copy of the LATEST version and
    timestamp per gating document (see ``legal.constants.denormalized_fields``).
    That copy is what every request reads; this table is what an audit reads.
    The copy is derived and could be rebuilt from here — never the reverse.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="legal_acceptances",
    )

    # A key from ``legal.constants.LEGAL_DOCUMENTS`` — in practice "terms" or
    # "privacy", the two that gate the product. Stored as free text rather than
    # choices so that retiring or renaming a document in the registry cannot
    # invalidate history that was true when it was written.
    document = models.CharField(max_length=20)
    version = models.CharField(max_length=20)

    # auto_now_add, so a re-recorded acceptance (get_or_create finds the row)
    # keeps the moment the user ACTUALLY agreed, not the moment we asked again.
    accepted_at = models.DateTimeField(auto_now_add=True, db_index=True)

    # Both are best-effort context captured from the request, not identity.
    # Null/blank whenever the caller had none — a service call from a shell or
    # a management command is still a real acceptance.
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=500, blank=True)

    class Meta:
        db_table = "legal_acceptances"
        ordering = ["-accepted_at"]
        indexes = [
            # "What is this user's most recent acceptance of this document" —
            # the only read shape there is, and the one a rebuild of the
            # denormalized columns on User would run per user.
            models.Index(
                fields=["user", "document", "-accepted_at"],
                name="legal_user_doc_recent_idx",
            ),
        ]
        constraints = [
            # Idempotency, enforced by the database rather than by the service
            # checking first. Recording the same acceptance twice — a double
            # tap, a retried request — must not produce two rows, because two
            # rows would read as two separate agreements to the same text.
            models.UniqueConstraint(
                fields=["user", "document", "version"],
                name="unique_acceptance_per_version",
            ),
        ]

    def __str__(self):
        return f"{self.user_id} accepted {self.document} {self.version}"
