from django.core.mail import EmailMessage
from django.conf import settings
import time
import threading

def send_email(
    subject, message, to_email, html_message=None, from_email=None, max_attempts=3, delay_seconds=2
    ):
    """
    Internal function: runs in background thread
    """
    if from_email is None:
        from_email = f"LearningMate AI <{settings.DEFAULT_FROM_EMAIL}>"

    if isinstance(to_email, str):
        to_email = [to_email]

    for attempt in range(1, max_attempts + 1):
        try:
            email = EmailMessage(
                subject=subject,
                body=message,
                from_email=from_email,
                to=to_email,
            )

            if html_message:
                email.attach_alternative(html_message, "text/html")

            response = email.send(fail_silently=False)

            if response:
                print(f"Email sent to {to_email}")
                return True

        except Exception as e:
            print(f"Attempt {attempt} failed: {str(e)}")

            if attempt < max_attempts:
                time.sleep(delay_seconds)

    print(f" All attempts failed for {to_email}")
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
        args=(subject, message, to_email, html_message, from_email, max_attempts, delay_seconds),
    )
    thread.daemon = True
    thread.start()