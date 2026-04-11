from firebase_admin import messaging
from notifications.models import UserFCMToken


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
            if not res.success:
                UserFCMToken.objects.filter(token=tokens[i]).update(is_active=False)



    