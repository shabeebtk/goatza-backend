import logging

from firebase_admin import messaging
from firebase_admin.exceptions import InvalidArgumentError
from notifications.models import UserFCMToken

logger = logging.getLogger(__name__)

# The only two failures that say anything about the TOKEN rather than about the
# network: the device unsubscribed, or the token isn't a token. Everything else
# (timeouts, 5xx, quota) is transient — deactivating on those silently
# unsubscribes a working browser until the user re-grants permission, which they
# have no reason to think they need to do.
#
# UnregisteredError is re-exported by firebase_admin.messaging;
# InvalidArgumentError only exists on firebase_admin.exceptions.
DEAD_TOKEN_ERRORS = (messaging.UnregisteredError, InvalidArgumentError)


class FCMService:

    @staticmethod
    def send_to_user(user, data: dict):
        tokens = list(
            UserFCMToken.objects.filter(user=user, is_active=True)
            .values_list("token", flat=True)
        )

        if not tokens:
            return

        message = messaging.MulticastMessage(
            tokens=tokens,
            data={k: str(v) for k, v in data.items()}  # must be string
        )

        response = messaging.send_each_for_multicast(message)

        # deactivate invalid tokens
        for i, res in enumerate(response.responses):
            if res.success:
                continue

            if isinstance(res.exception, DEAD_TOKEN_ERRORS):
                UserFCMToken.objects.filter(token=tokens[i]).update(is_active=False)
            else:
                logger.warning(
                    "FCMService | send failed, token kept active | %s",
                    res.exception,
                )
