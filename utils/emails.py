from django.core.mail import EmailMessage
from django.conf import settings
import time

def send_email(
    subject, message, to_email, html_message=None, from_email=None, max_attempts=3, delay_seconds=2
    ):
    """
    Send email with retry logic.
    Tries up to max_attempts, waiting delay_seconds between attempts.
    Returns True if email sent successfully, False otherwise.
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
            if response:  # 1 if success
                return True
        except Exception as e:
            print(f"Attempt {attempt}: Error sending email: {e}")
            if attempt < max_attempts:
                time.sleep(delay_seconds)  # wait before retrying

    return False
