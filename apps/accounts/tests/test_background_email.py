"""
Transactional email now leaves through a task, not a daemon thread.

WHAT BROKE BEFORE. ``send_email_async`` started a ``threading.Thread`` and
returned. A gunicorn restart — a Render deploy, a SIGTERM on an idle instance —
killed that thread wherever it happened to be, routinely between
``time.sleep(delay_seconds)`` and the next POST to Resend. The mail was gone
with nothing but two attempt WARNINGs behind it, and for an OTP that is a
person who cannot finish signing up and has no other channel to tell us.

So the first test here is a REGRESSION GUARD and not really a behaviour test:
no thread may be started on this path, ever again.

The signature is unchanged and the "returns None" contract is unchanged, which
is what lets ``utils.transactional_emails`` keep calling it exactly as it did.
"""

import json
import threading
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings

from utils.emails import send_email_async, send_email_task

SUBJECT = "Your Goatza verification code"
BODY = "482913"
TO = "arjun@example.com"

# The nine arguments send_email_async forwards. Named here because the set is
# the contract between the caller and the worker: anything added to one side
# and not the other is a TypeError in a worker nobody is watching.
EXPECTED_KWARGS = {
    "subject", "message", "to_email", "html_message", "from_email",
    "max_attempts", "delay_seconds", "template", "is_otp",
}


def _resend(status_code, text="{}"):
    response = MagicMock()
    response.status_code = status_code
    response.text = text
    return response


class SendEmailAsyncTests(TestCase):
    """The dispatch side: what happens in the request."""

    def test_no_thread_is_ever_started(self):
        # THE REGRESSION GUARD. utils.emails does not import threading any
        # more, and this is what notices if it comes back.
        with patch.object(threading, "Thread") as thread:
            with patch("utils.emails.requests.post", return_value=_resend(200)):
                with self.captureOnCommitCallbacks(execute=True):
                    send_email_async(SUBJECT, BODY, TO)

        thread.assert_not_called()

    @override_settings(CELERY_ENABLED=False)
    def test_with_celery_off_the_mail_is_sent_inline(self):
        # The behaviour a checkout with no broker and no worker must keep: the
        # job runs where it was called, so the OTP still goes out.
        with patch("utils.emails.requests.post", return_value=_resend(200)) as post:
            with self.captureOnCommitCallbacks(execute=True):
                result = send_email_async(SUBJECT, BODY, TO, html_message="<p>x</p>")

        self.assertIsNone(result)          # the contract, unchanged
        post.assert_called_once()
        sent = post.call_args.kwargs["json"]
        self.assertEqual(sent["subject"], SUBJECT)
        self.assertEqual(sent["to"], [TO])

    @override_settings(CELERY_ENABLED=False)
    def test_nothing_is_dispatched_before_the_transaction_commits(self):
        # enqueue's on_commit default. A mail about a row that never committed
        # is a mail about something that did not happen.
        with patch("utils.emails.requests.post") as post:
            with self.captureOnCommitCallbacks(execute=False):
                send_email_async(SUBJECT, BODY, TO)

            post.assert_not_called()

    @override_settings(CELERY_ENABLED=True)
    def test_with_celery_on_it_is_published_and_not_sent_here(self):
        with patch.object(send_email_task, "apply_async") as apply_async:
            with patch("utils.emails.requests.post") as post:
                with self.captureOnCommitCallbacks(execute=True):
                    send_email_async(
                        SUBJECT, BODY, TO,
                        html_message="<p>482913</p>",
                        template="emails/otp.html",
                        is_otp=True,
                    )

        apply_async.assert_called_once()
        post.assert_not_called()           # queued means NOT also sent here

    @override_settings(CELERY_ENABLED=True)
    def test_every_published_argument_is_json_serialisable(self):
        """
        The kwargs cross a JSON boundary, so a model instance or a lazy
        translation among them is a publish-time failure in a worker — not here
        where somebody would see it.
        """
        with patch.object(send_email_task, "apply_async") as apply_async:
            with self.captureOnCommitCallbacks(execute=True):
                send_email_async(
                    SUBJECT, BODY, TO,
                    html_message="<p>x</p>",
                    template="emails/otp.html",
                    is_otp=True,
                )

        published = apply_async.call_args.kwargs
        self.assertEqual(published["args"], ())
        self.assertEqual(set(published["kwargs"]), EXPECTED_KWARGS)

        # The real assertion: the payload survives the serializer.
        json.dumps(published["kwargs"])

        for key, value in published["kwargs"].items():
            self.assertIsInstance(
                value, (str, int, bool, type(None), list),
                msg=f"{key} is not a JSON primitive",
            )


class SendEmailTaskTests(TestCase):
    """The worker side: what the task does with a Resend that will not answer."""

    # delay_seconds=0 throughout. The real default sleeps 2s between attempts,
    # which is correct in production and four wasted seconds in a test.

    def test_a_failing_resend_is_retried_the_configured_number_of_times(self):
        with patch(
            "utils.emails.requests.post", return_value=_resend(500, "upstream boom")
        ) as post:
            with self.assertLogs("utils.emails", level="ERROR"):
                send_email_task(
                    subject=SUBJECT, message=BODY, to_email=TO,
                    max_attempts=4, delay_seconds=0,
                )

        self.assertEqual(post.call_count, 4)

    def test_permanent_failure_logs_an_error_and_raises_nothing(self):
        """
        ERROR IS THE POINT. The Sentry logging integration turns ERROR records
        into events, and this line is the only trace a lost email leaves —
        nothing above the task is waiting on the result.
        """
        with patch("utils.emails.requests.post", return_value=_resend(422, "bad")):
            with self.assertLogs("utils.emails", level="ERROR") as logs:
                # No exception: send_email absorbs every attempt itself and
                # the task adds no retry on top of its three.
                send_email_task(
                    subject=SUBJECT, message=BODY, to_email=TO,
                    max_attempts=3, delay_seconds=0,
                    template="emails/otp.html", is_otp=True,
                )

        permanent = [line for line in logs.output if "PERMANENTLY FAILED" in line]
        self.assertEqual(len(permanent), 1)
        # otp=True is the field somebody searches Sentry by: a lost OTP is a
        # person locked out, not a missed notification.
        self.assertIn("otp=True", permanent[0])

    def test_the_address_is_masked_in_every_log_line(self):
        # Render's log stream is not a place for user contact details, and many
        # of this app's users are minors.
        with patch("utils.emails.requests.post", return_value=_resend(500)):
            with self.assertLogs("utils.emails", level="INFO") as logs:
                send_email_task(
                    subject=SUBJECT, message=BODY, to_email=TO,
                    max_attempts=1, delay_seconds=0,
                )

        joined = "\n".join(logs.output)
        self.assertNotIn(TO, joined)
        self.assertIn("a***n@example.com", joined)

    def test_a_successful_send_logs_one_completion_line_with_counts(self):
        with patch("utils.emails.requests.post", return_value=_resend(200)):
            with self.assertLogs("utils.emails", level="INFO") as logs:
                result = send_email_task(
                    subject=SUBJECT, message=BODY, to_email=[TO, "b@example.com"],
                    delay_seconds=0,
                )

        self.assertTrue(result)
        done = [line for line in logs.output if "emails.send | done" in line]
        self.assertEqual(len(done), 1)
        # Resend takes every recipient in one request, so it is all-or-nothing.
        self.assertIn("tried=2 | sent=2 | failed=0", done[0])

    def test_a_structural_failure_is_logged_at_error_and_re_raised(self):
        # send_email catches its own per-attempt failures, so reaching here
        # means something else — a bad argument, a missing setting. Celery
        # should see it; the log line should name the email.
        with patch("utils.emails.send_email", side_effect=TypeError("bad arg")):
            with self.assertLogs("utils.emails", level="ERROR") as logs:
                with self.assertRaises(TypeError):
                    send_email_task(subject=SUBJECT, message=BODY, to_email=TO)

        self.assertIn("emails.send | FAILED", logs.output[0])

    def test_acks_late_is_off_so_a_crash_cannot_send_twice(self):
        # An email is not idempotent and there is no row to mark as sent, so a
        # worker dying between the POST and the ack must lose at most one mail
        # rather than deliver a second copy — a duplicate OTP invalidates the
        # code the person is already typing.
        self.assertFalse(send_email_task.acks_late)
