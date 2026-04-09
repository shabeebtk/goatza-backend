import requests
from django.conf import settings
import time
import threading


def send_email(
    subject,
    message,
    to_email,
    html_message=None,
    from_email=None,
    max_attempts=3,
    delay_seconds=2,
):
    """
    Internal function: runs in background thread (Resend)
    """

    if from_email is None:
        from_email = settings.RESEND_FROM_EMAIL

    if isinstance(to_email, str):
        to_email = [to_email]

    for attempt in range(1, max_attempts + 1):
        try:
            response = requests.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {settings.RESEND_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "from": from_email,
                    "to": to_email,
                    "subject": subject,
                    "text": message,
                    "html": html_message,
                },
                timeout=10,  
            )

            if response.status_code in [200, 201]:
                print(f"Email sent to {to_email}")
                return True
            else:
                print(f"Attempt {attempt} failed: {response.text}")

        except Exception as e:
            print(f"Attempt {attempt} exception: {str(e)}")

        if attempt < max_attempts:
            time.sleep(delay_seconds)

    print(f"All attempts failed for {to_email}")
    return False


def send_email_async(
    subject,
    message,
    to_email,
    html_message=None,
    from_email=None,
    max_attempts=3,
    delay_seconds=2,
):
    """
    Public function: non-blocking email sender
    """
    thread = threading.Thread(
        target=send_email,
        args=(
            subject,
            message,
            to_email,
            html_message,
            from_email,
            max_attempts,
            delay_seconds,
        ),
    )
    thread.daemon = True
    thread.start()