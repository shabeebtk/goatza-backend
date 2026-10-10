"""The one place mail leaves this app: an HTTP POST to Resend, retried.

Django's SMTP backend is deliberately unconfigured (see the EMAIL block in
core/settings.py); nothing in the codebase calls ``send_mail`` or builds an
``EmailMessage``.

WHY THE LOGGING HERE MATTERS MORE THAN IT LOOKS: ``send_email_async`` runs this
OFF THE REQUEST — in a Celery worker, or right here inline when Celery is off or
the broker is in cooldown (``utils.background_jobs``). Nothing awaits it, no
caller can react to it, and nothing retries it afterwards — when the last
attempt fails, that email is gone for good. This module's log lines are the ONLY
trace it ever existed, which is why they are logger calls and not prints: a
print inside a worker on Render is a line in a stdout stream nobody greps, with
no level, no timestamp and no route into Sentry.
"""

import logging
import time

import requests
from celery import shared_task
from django.conf import settings

from utils.background_jobs import enqueue

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
    Internal function: the blocking send (Resend), retried in-process.

    Runs wherever it is called — a worker (``send_email_task``), a management
    command draining the announcement outbox, or the request thread itself via
    enqueue's inline fallback. It is the only function here that talks to
    Resend, and the only one that decides the retry and log policy.

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

    # PERMANENT LOSS. On the async path nothing above this frame is waiting on
    # the result — it is a worker running ``emails.send``, and the task adds no
    # retry of its own — so this line is the last thing that happens to this
    # email.
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


@shared_task(name="emails.send", acks_late=False)
def send_email_task(
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
    Send one email in the worker. A thin wrapper around ``send_email``, so the
    retry and log policy above stays in one place and behaves identically from
    a worker, a management command or enqueue's inline fallback.

    ``acks_late=False`` ON PURPOSE, against the project-wide ``task_acks_late``
    default (CLAUDE.md, "Background jobs"), and this is the one place that
    inverts it. An email is NOT idempotent and there is no row to mark as sent,
    so the usual "make the second run a no-op" has nothing to hang on. With
    late acks, a worker dying between the POST to Resend and the ack hands the
    job to the next worker and the person receives the same mail twice \u2014 and a
    duplicate OTP is worse than a missing one, because the second code
    invalidates the first one the person is already typing. Acking up front
    makes this AT MOST ONCE: a crash in that window loses the email, which is
    the failure the ERROR line in ``send_email`` exists to record.

    NO ``autoretry_for`` / ``retry_backoff`` HERE, deliberately. ``send_email``
    already makes ``max_attempts`` (3) attempts of its own with
    ``delay_seconds`` between them, and that is what covers a flaky Resend. A
    task-level retry on top would be 3 \u00d7 3 = nine POSTs for one email, and
    Resend answering 422 nine times is still a 422.

    Every argument is JSON \u2014 see the note on ``send_email_async``.
    """
    # Only for the log line: send_email does this same normalisation internally.
    recipients = [to_email] if isinstance(to_email, str) else list(to_email or [])
    template_name = template or "adhoc"

    try:
        sent = send_email(
            subject=subject,
            message=message,
            to_email=to_email,
            html_message=html_message,
            from_email=from_email,
            max_attempts=max_attempts,
            delay_seconds=delay_seconds,
            template=template,
            is_otp=is_otp,
        )
    except Exception:
        # send_email catches per-attempt failures itself and returns False, so
        # reaching here means something structural (a bad argument, a missing
        # setting). ERROR carries it to Sentry naming the email rather than as
        # a bare Celery traceback; the re-raise lets Celery mark it failed too.
        logger.error(
            "emails.send | FAILED | template=%s | otp=%s | to=%s",
            template_name, is_otp, _mask_recipients(recipients),
            exc_info=True,
        )
        raise

    # Resend takes all recipients in one request, so delivery is all-or-nothing
    # for this call \u2014 hence the counts are 0 or len(recipients), never between.
    logger.info(
        "emails.send | done | template=%s | otp=%s | to=%s | tried=%s | "
        "sent=%s | failed=%s",
        template_name, is_otp, _mask_recipients(recipients),
        len(recipients),
        len(recipients) if sent else 0,
        0 if sent else len(recipients),
    )
    return sent


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

    Hands the mail to ``emails.send`` through ``utils.background_jobs.enqueue``
    \u2014 a worker when Celery is on and the broker is healthy, inline right here
    when it is not, so the behaviour with no broker is what it always was.

    THIS USED TO BE A DAEMON THREAD. A gunicorn restart (Render deploys, SIGTERM
    on an idle dyno) killed it mid-retry, between ``time.sleep(delay_seconds)``
    and the next POST, and the email was gone with only the attempt WARNINGs
    behind it. In the worker the job survives the web process.

    RETURNS None, and that is the contract. ``utils.transactional_emails._send``
    reads a return value only on the blocking path (``sender`` is ``send_email``
    there) and returns a flat ``True`` on this one; nothing can know the outcome
    here, which is the point of the function. With enqueue's default
    ``on_commit=True`` the publish has not even happened yet when this returns.

    EVERY ARGUMENT IS A JSON PRIMITIVE \u2014 str, int, bool, None, or a list of str
    for ``to_email``. That is a requirement, not a style note: these kwargs are
    serialised to JSON at publish time, so a caller passing a model instance, a
    lazy translation or a datetime would raise inside ``apply_async``. enqueue
    treats that as a publish failure, which means the email still goes out
    inline and leaves an ERROR line naming the task \u2014 but it also parks the
    broker for the cooldown, so it is worth not doing.
    """
    enqueue(
        send_email_task,
        kwargs={
            "subject": subject,
            "message": message,
            "to_email": to_email,
            "html_message": html_message,
            "from_email": from_email,
            "max_attempts": max_attempts,
            "delay_seconds": delay_seconds,
            "template": template,
            "is_otp": is_otp,
        },
    )
