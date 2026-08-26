"""
Domain errors for the messaging services.

Each carries a stable machine-readable ``reason``. The share endpoint reports
per-target failures as {"id": …, "reason": …}, so these codes are part of the
API contract — rename them and clients break. The human-facing string is the
exception message.
"""


class MessageError(Exception):
    """Base class for a message that could not be sent."""
    reason = "error"


class NotParticipantError(MessageError):
    reason = "not_a_participant"


class EmptyMessageError(MessageError):
    reason = "empty_message"


class InvalidSenderError(MessageError):
    reason = "invalid_sender"


class ContentUnavailableError(MessageError):
    """The shared object is deleted, or not visible to the sender."""
    reason = "content_unavailable"


class InvalidMediaError(MessageError):
    """The media URL/public_id is not a trusted chat upload from our cloud."""
    reason = "invalid_media"


class ConversationNotFoundError(MessageError):
    reason = "conversation_not_found"


class MessageNotFoundError(MessageError):
    """No such message in this conversation (or it is already deleted)."""
    reason = "message_not_found"


class NotMessageSenderError(MessageError):
    """Only the actor who sent a message may unsend it."""
    reason = "not_message_sender"


class RecipientNotFoundError(MessageError):
    reason = "recipient_not_found"


class SelfShareError(MessageError):
    reason = "cannot_share_with_self"


class BlockedParticipantError(MessageError):
    """
    A block exists between the sender and the other party, in either
    direction.

    A MessageError rather than a bare 403 so the multi-target share endpoint
    reports it against the one recipient it applies to and still delivers to
    everybody else. The human message is the generic one every block guard
    uses (moderation.services.block_guard.BLOCKED_MESSAGE) — the reason code
    is for the client's own logic, not for display.
    """
    reason = "blocked"
