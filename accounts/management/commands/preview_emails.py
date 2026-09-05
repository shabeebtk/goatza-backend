"""Render every transactional email to ./email_previews/ for eyeballing.

The templates are the only thing standing between a code change and a mail
nobody can un-send, and Resend is not a review tool. This renders them all with
the sample data from ``email-template-reference/README.md`` so the output can be
diffed — visually, in a browser — against the approved reference files.

Adding an email means adding one PREVIEWS entry. Nothing here is imported by
the app; it is a local tool.
"""

import io
from datetime import datetime, timezone as dt_timezone
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.template.loader import render_to_string
from django.utils.safestring import mark_safe

from utils.transactional_emails import (
    APPLICATION_RECEIVED_TEMPLATE,
    APPLICATION_STATUS_TEMPLATE,
    LOGIN_OTP_COPY,
    NEW_APPLICANT_ALERT_TEMPLATE,
    OTP_TEMPLATE,
    PASSWORD_CHANGED_SUBJECT,
    PASSWORD_CHANGED_TEMPLATE,
    PASSWORD_RESET_OTP_COPY,
    SIGNUP_OTP_COPY,
    STATUS_EMAIL_COPY,
    WELCOME_SUBJECT,
    WELCOME_TEMPLATE,
    format_ist_timestamp,
    initials,
    meta_tail,
    shared_email_context,
)

OUTPUT_DIR = "email_previews"

# Sample values are the ones the reference files were approved with, so a
# preview and its reference differ only where the template is wrong.
NAME = "Arjun"
# Fixed, so a preview only ever changes when a template does — and it is
# the instant the reference files were approved with: 6:42 PM IST.
SAMPLE_CHANGED_AT = datetime(2026, 9, 4, 13, 12, tzinfo=dt_timezone.utc)

# Recruitment sample set, straight from email-template-reference/README.md.
ORG_NAME = "Trivandrum City FC"
ORG_ID = "4d7e2b"
RECRUITMENT_ID = "8f2c1a"
RECRUITMENT_TITLE = "U-19 Striker Trials \u2014 Season 2026"
POSITION = "Striker"
LOCATION = "Thiruvananthapuram, Kerala"
APPLIED_DATE = "4 Sep 2026"
PLAYER_FULL_NAME = "Arjun Menon"
PLAYER_USERNAME = "arjunmenon10"
PLAYER_AGE = 17

# The card block shared by the received + status emails.
RECRUITMENT_CARD = {
    "org_initials": initials(ORG_NAME),
    "recruitment_title": RECRUITMENT_TITLE,
    "org_name": ORG_NAME,
    "card_meta_tail": meta_tail([POSITION, LOCATION], leading=True),
    "recruitment_id": RECRUITMENT_ID,
}

# The card block shared by both modes of the org alert.
APPLICANT_CARD = {
    "applicant_initials": initials(PLAYER_FULL_NAME),
    "player_name": PLAYER_FULL_NAME,
    "player_username": PLAYER_USERNAME,
    "applicant_meta": meta_tail([POSITION, PLAYER_AGE, LOCATION]),
}


def _status_preview(to_status):
    """One (filename, template, context) entry per application status."""
    copy = STATUS_EMAIL_COPY[to_status]
    return (
        APPLICATION_STATUS_TEMPLATE,
        {
            **RECRUITMENT_CARD,
            "subject": copy["subject"].format(title=RECRUITMENT_TITLE),
            "to_status": to_status,
            "player_name": NAME,
            "badge_label": copy["badge_label"],
            "badge_style": mark_safe(copy["badge_style"]),
        },
    )


def _alert_preview(new_count, total_count):
    """One entry for each mode of the org alert — single and rollup."""
    is_rollup = new_count > 1
    subject = (
        f"{new_count} new applicants \u2014 {RECRUITMENT_TITLE}"
        if is_rollup
        else f"New applicant: {PLAYER_FULL_NAME} \u2014 {RECRUITMENT_TITLE}"
    )
    return (
        NEW_APPLICANT_ALERT_TEMPLATE,
        {
            **APPLICANT_CARD,
            "subject": subject,
            "is_rollup": is_rollup,
            "new_count": new_count,
            "more_count": new_count - 1,
            "total_count": total_count,
            "organization_id": ORG_ID,
            "org_name": ORG_NAME,
            "recruitment_title": RECRUITMENT_TITLE,
        },
    )

PREVIEWS = [
    (
        "01-signup-otp.html",
        OTP_TEMPLATE,
        {**SIGNUP_OTP_COPY, "name": NAME, "otp": "482913"},
    ),
    (
        "02-login-otp.html",
        OTP_TEMPLATE,
        {**LOGIN_OTP_COPY, "name": NAME, "otp": "173248"},
    ),
    (
        "03-password-reset-otp.html",
        OTP_TEMPLATE,
        {**PASSWORD_RESET_OTP_COPY, "name": NAME, "otp": "905617"},
    ),
    (
        "04-welcome.html",
        WELCOME_TEMPLATE,
        {"subject": WELCOME_SUBJECT.format(name=NAME), "name": NAME},
    ),
    (
        "05-password-changed.html",
        PASSWORD_CHANGED_TEMPLATE,
        {
            "subject": PASSWORD_CHANGED_SUBJECT,
            "name": NAME,
            "changed_at": format_ist_timestamp(SAMPLE_CHANGED_AT),
        },
    ),
    (
        "06-application-received.html",
        APPLICATION_RECEIVED_TEMPLATE,
        {
            **RECRUITMENT_CARD,
            "subject": f"Application sent: {RECRUITMENT_TITLE}",
            "player_name": NAME,
            "applied_date": APPLIED_DATE,
        },
    ),
    ("07a-status-selected.html", *_status_preview("selected")),
    ("07b-status-shortlisted.html", *_status_preview("shortlisted")),
    ("07c-status-rejected.html", *_status_preview("rejected")),
    ("07d-status-invited.html", *_status_preview("invited")),
    ("08a-new-applicant.html", *_alert_preview(new_count=1, total_count=12)),
    (
        "08b-new-applicants-rollup.html",
        *_alert_preview(new_count=4, total_count=16),
    ),
]


class Command(BaseCommand):
    help = "Render every transactional email template to ./email_previews/"

    def handle(self, *args, **options):
        out_dir = Path(settings.BASE_DIR) / OUTPUT_DIR
        out_dir.mkdir(parents=True, exist_ok=True)

        for filename, template, context in PREVIEWS:
            html = render_to_string(
                template,
                {**context, **shared_email_context()},
            )

            path = out_dir / filename
            # newline="" keeps the LF the templates hold, so a preview
            # diffs cleanly against email-template-reference/ on Windows.
            with io.open(path, "w", encoding="utf-8", newline="") as fh:
                fh.write(html)

            self.stdout.write(str(path))

        self.stdout.write(
            self.style.SUCCESS(f"Rendered {len(PREVIEWS)} email previews")
        )
