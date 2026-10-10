"""
The one way a websocket fan-out leaves a service: ``safe_group_send``.

WHY THIS EXISTS. ``channel_layer.group_send`` talks to Redis, and every service
that called it did so bare, on the request thread, right after saving a row.
The comments around those calls all said the same thing — a dead Redis must
never roll back a saved message — and they were right that it did not: the row
was committed before the send. But the exception still escaped, the view
answered 500, and the sender was told their message had FAILED. So they sent it
again. One dead Redis produced duplicate messages and a chat that looked broken
while every single message was safely in Postgres.

A REALTIME SEND IS A COURTESY, NOT A WRITE. It saves the other side a refetch,
nothing more: every screen that depends on one also loads the same state over
HTTP (the thread fetch, the conversation list, ``is_read``). So the correct
behaviour when the channel layer is gone is to lose the nudge, log it, and let
the request succeed \u2014 the client catches up on its next fetch or reconnect.

``safe_group_send`` NEVER RAISES. That is the entire contract, and it is why
callers no longer need a try/except of their own.
"""

import logging

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

logger = logging.getLogger(__name__)


def safe_group_send(group, message, *, tag) -> bool:
    """
    Send ``message`` to channel-layer ``group``. True if it went, False if not.

    ``tag`` names the event in the log line \u2014 ``"messaging.chat_message"``,
    ``"messaging.conversation_read"`` \u2014 so a failure says WHICH fan-out was
    lost, not just that one was. It is required and keyword-only: the whole
    value of these lines is being able to tell them apart, and a positional
    argument is the kind of thing that gets passed in the wrong order once and
    then lies in the logs forever.

    The return value is for a caller that wants to count or branch; nobody has
    to read it, and most do not.

    ``message`` must be msgpack-able \u2014 primitives only, no datetimes and no
    model instances. That is the channel layer's rule, not this function's, and
    breaking it raises here like any other failure: logged, swallowed, False.
    """
    try:
        channel_layer = get_channel_layer()

        if channel_layer is None:
            # No CHANNEL_LAYERS configured at all. Not a failure of anything
            # running, so WARNING: there is nothing listening either.
            logger.warning(
                "%s | realtime send skipped, no channel layer | group=%s",
                tag, group,
            )
            return False

        async_to_sync(channel_layer.group_send)(group, message)
    except Exception:
        # ERROR, so the Sentry logging integration raises an event: the request
        # is about to succeed and nothing else will ever mention this, which is
        # exactly the kind of silent degradation that goes unnoticed for weeks.
        #
        # Per call, deliberately NOT throttled the way the broker and cache
        # failures are. Those two are checked before every single operation in
        # the app; a realtime send happens once per message, and a conversation
        # with ten participants is eleven lines, not eleven hundred.
        logger.error(
            "%s | realtime send failed | group=%s", tag, group, exc_info=True
        )
        return False

    return True
