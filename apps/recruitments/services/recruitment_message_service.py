# recruitments/services/recruitment_message_service.py
"""
Sending a recruitment's updates INTO Goatza, as real messages.

WHY GOATZA AND NOT WHATSAPP. The messaging module already has org↔user direct
threads, per-participant unread state, live WebSocket delivery and — the
useful part — a ``shared_recruitment`` message type the client already renders
as a card. Routing every trial update to ``wa.me`` instead hands the org the
player's phone number and gives neither of them a reason to come back.
WhatsApp stays a fallback, offered in the UI.

THE REQUEST-GATE PROBLEM. ``ConversationService`` opens a thread as REQUESTED
unless the two follow each other, and a trial update that lands in a
message-request folder has failed at the only job it had. So this service is
the ONE caller of ``get_or_create_conversation(force_active=True)`` — and
``_assert_allowed`` below is what keeps it the only one: the sender must be an
organization and the recipient must have a live application on the recruitment
being messaged. Applying is a stronger, more specific consent signal than the
mutual follow the gate approximates with.

A BLOCK STILL WINS. ``get_or_create_conversation`` runs the block guard before
anything else and force_active does not touch it. A blocked pair raises, this
service catches it PER RECIPIENT, and the fan-out carries on — one block must
never end a send to 140 other people.

NOTHING HERE IS CALLED FROM A REQUEST. Both callers are the outbox drain
(``dispatch_announcements``), for the same reason the email channel is: see
``AnnouncementDelivery``.
"""

import logging

from apps.messaging.models import Message
from apps.messaging.services.conversation_service import ConversationService
from apps.messaging.services.message_service import MessageService
from apps.recruitments.models import RecruitmentApplication

logger = logging.getLogger(__name__)


class RecruitmentMessageError(Exception):
    """One recipient could not be messaged. Never ends the fan-out."""


class RecruitmentMessageService:

    @staticmethod
    def _assert_allowed(recruitment, application):
        """
        The scope check that makes ``force_active`` safe to exist.

        Two facts, both required: the sender is the organization that owns
        this recruitment, and the recipient has a LIVE (non-withdrawn)
        application on it. A withdrawn applicant took themselves off the list
        and does not get the request gate lifted for them.

        Raises rather than returning False: this is an invariant of the one
        place allowed to bypass the gate, not a routine per-recipient
        condition, and a caller that gets it wrong should be unable to send at
        all.
        """
        if application.recruitment_id != recruitment.id:
            raise RecruitmentMessageError(
                "Application does not belong to this recruitment."
            )

        if application.status == RecruitmentApplication.Status.WITHDRAWN:
            raise RecruitmentMessageError("Applicant withdrew.")

    @staticmethod
    def send_one(recruitment, application, *, body, message_type):
        """
        Message ONE applicant. Returns the Message.

        Everything goes through ``MessageService`` and never writes a Message
        row by hand: that service owns the conversation's last-message cache,
        the unread state, the WebSocket dispatch and the message notification,
        and a hand-written row would silently skip all four.
        """
        RecruitmentMessageService._assert_allowed(recruitment, application)

        organization = recruitment.organization

        conversation, _ = ConversationService.get_or_create_conversation(
            actor_org=organization,
            target_user=application.applicant,
            # The one place this is passed. See the module docstring.
            force_active=True,
        )

        if message_type == Message.Type.SHARED_RECRUITMENT:
            # The card plus the update as its caption. send_shared_recruitment
            # is what attaches the FK — send_message cannot, and the DB
            # CheckConstraint requires the right FK set with every other
            # shared_* column null, which this path satisfies by construction.
            return MessageService.send_shared_recruitment(
                conversation,
                sender_org=organization,
                recruitment=recruitment,
                note=body,
            )

        return MessageService.send_message(
            conversation,
            sender_org=organization,
            content=body,
            message_type=Message.Type.TEXT,
        )

    @staticmethod
    def fan_out(
        recruitment,
        applications,
        *,
        body,
        message_type,
        created_by_member=None,
    ):
        """
        Message every applicant in ``applications``.

        Returns ``(sent, skipped)`` — the applications that got a message, and
        ``(application, reason)`` for the ones that did not. A blocked pair,
        an unavailable recruitment and a withdrawn applicant are all per
        recipient: the fan-out finishes either way, because a single block
        must never cost the other 140 people their trial update.

        ``created_by_member`` is accepted for the caller's audit trail and
        deliberately not written onto the Message: a message is FROM THE
        ORGANIZATION, and the recipient has no business knowing which staff
        member pressed send.
        """
        sent = []
        skipped = []

        for application in applications:
            try:
                message = RecruitmentMessageService.send_one(
                    recruitment,
                    application,
                    body=body,
                    message_type=message_type,
                )
                sent.append((application, message))
            except Exception as exc:
                # Deliberately broad. Block guards, unavailable content, an
                # empty body and a messaging-layer failure all mean the same
                # thing here — this recipient did not get it, the next one
                # still might.
                skipped.append((application, f"{type(exc).__name__}: {exc}"))
                logger.warning(
                    "RecruitmentMessageService.fan_out | skipped | "
                    "recruitment_id=%s | application_id=%s | %s",
                    recruitment.id, application.id, exc,
                )

        return sent, skipped
