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


class RecipientNotFoundError(MessageError):
    reason = "recipient_not_found"


class SelfShareError(MessageError):
    reason = "cannot_share_with_self"
