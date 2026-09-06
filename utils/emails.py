"""The one place mail leaves this app: an HTTP POST to Resend, retried.

Django's SMTP backend is deliberately unconfigured (see the EMAIL block in
core/settings.py); nothing in the codebase calls ``send_mail`` or builds an
``EmailMessage``.

WHY THE LOGGING HERE MATTERS MORE THAN IT LOOKS: ``send_email_async`` runs this
in a DAEMON THREAD, off the request. Nothing awaits it, no caller can react to
it, and nothing retries it afterwards — when the last attempt fails, that email
is gone for good. This module's log lines are the ONLY trace it ever existed,
which is why they are logger calls and not prints: a print inside a background
thread on Render is a line in a stdout stream nobody greps, with no level, no
timestamp and no route into Sentry.
"""

import logging
import threading
import time

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

RESEND_ENDPOINT = "https://api.resend.com/emails"


def mask_email(address):
    """
    ``"shabeeb@example.com"`` -> ``"s*****b@example.com"``.

    Enough to recognise an address you already have in front of you (support
    ticket, admin page) and not enough to harvest one out of the logs. Render's
    log stream is not a place for user contact details, and this app's users
    are athletes, many of them minors — the same reasoning as
    ``send_default_pii=False`` on the Sentry init.

    The domain is kept whole because it is the diagnostic half: "every failure
    is to one provider" is the shape of a real outage.
    """
    if not address or not isinstance(address, str):
        return "<unknown>"

    local, separator, domain = address.partition("@")
    if not separator:
        return "<malformed>"

    if len(local) <= 2:
        return f"{local[:1]}*@{domain}"

    return f"{local[0]}{'*' * (len(local) - 2)}{local[-1]}@{domain}"


def _mask_recipients(to_email):
    return ", ".join(mask_email(address) for address in to_email)


def send_email(
    subject,
    message,
    to_email,
    html_message=None,
    from_email=None,
    max_attempts=3,
    delay_seconds=2,
    template=None,
    is_otp=False,
):
    """
    Internal function: runs in background thread (Resend)

    ``template`` and ``is_otp`` are for the log lines only and never reach
    Resend. ``utils.transactional_emails`` passes both; the handful of callers
    that build a one-off mail (waitlist notification, moderation alert) leave
    them at their defaults.
    """

    if from_email is None:
        from_email = settings.RESEND_FROM_EMAIL

    if isinstance(to_email, str):
        to_email = [to_email]

    recipients = _mask_recipients(to_email)
    template_name = template or "adhoc"

    # Kept so the final ERROR can carry a real traceback. Resend answering 422
    # three times raises nothing at all, and bare `exc_info=True` outside an
    # except block would render the useless "NoneType: None".
    last_exception = None

    for attempt in range(1, max_attempts + 1):
        try:
            response = requests.post(
                RESEND_ENDPOINT,
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
                logger.info(
                    "send_email | sent | template=%s | to=%s | attempt=%s",
                    template_name, recipients, attempt,
                )
                return True

            # WARNING, not ERROR: there are two more attempts coming and a
            # single 429 or 502 from Resend that the retry absorbs is not
            # something to wake anybody for. The response BODY is included
            # because it carries Resend's reason ("domain not verified",
            # "invalid to field") and that is usually the whole diagnosis.
            logger.warning(
                "send_email | attempt failed | template=%s | otp=%s | to=%s | "
                "attempt=%s/%s | status=%s | %s",
                template_name, is_otp, recipients, attempt, max_attempts,
                response.status_code, response.text,
            )

        except Exception as exc:
            last_exception = exc
            logger.warning(
                "send_email | attempt raised | template=%s | otp=%s | to=%s | "
                "attempt=%s/%s | %s",
                template_name, is_otp, recipients, attempt, max_attempts, exc,
            )

        if attempt < max_attempts:
            time.sleep(delay_seconds)

    # PERMANENT LOSS. Nothing above this frame is waiting on the result — this
    # runs in a daemon thread — so this line is the last thing that happens to
    # this email.
    #
    # ERROR ON PURPOSE, AND IT MUST STAY ERROR: the Sentry logging integration
    # is on by default whenever SENTRY_DSN is set (core/settings.py), and it
    # turns ERROR-level records into Sentry EVENTS. Downgrade this to a warning
    # while "tidying up the logs" and the alert disappears with it.
    #
    # ``otp=True`` is the field to search Sentry by. A lost notification is an
    # annoyance; a lost OTP is a person who cannot finish signing up, cannot log
    # in and cannot reset their password, and who has no way to tell us —
    # because the only channel we have to them is the one that just broke.
    logger.error(
        "send_email | PERMANENTLY FAILED after %s attempts, email is lost | "
        "template=%s | otp=%s | to=%s | subject=%r",
        max_attempts, template_name, is_otp, recipients, subject,
        exc_info=last_exception or True,
    )
    return False


def send_email_async(
    subject,
    message,
    to_email,
    html_message=None,
    from_email=None,
    max_attempts=3,
    delay_seconds=2,
    template=None,
    is_otp=False,
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
            template,
            is_otp,
        ),
    )
    thread.daemon = True
    thread.start()
