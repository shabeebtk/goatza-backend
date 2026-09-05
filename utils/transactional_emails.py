"""Transactional emails — one function per email the product sends.

Every function here owns three things for its email: the exact subject, the
plain-text body, and the context its HTML template renders from. Call sites
pass data, never copy — so wording changes land in one file and the text and
HTML versions can never drift apart.

Design source of truth is ``email-template-reference/`` at the repo root. The
per-email copy constants below are the strings from those files; the shared
shell lives in ``templates/emails/base.html``.

Sending is fire-and-forget through ``utils.emails.send_email_async`` (a daemon
thread posting to Resend). Nothing in here raises: an email is a side effect of
a request, and a template typo or a missing setting must not turn a successful
signup into a 500. Same swallow-and-log reasoning as ``_notify_org`` in
``recruitments/services/application_service.py``.

Copy constants are HTML fragments, entities and all, so the rendered mail is
byte-identical to the reference. That means they must NOT be autoescaped — see
``_copy`` for why marking only these static literals safe is safe.
"""

import logging
from datetime import timezone as datetime_timezone
from zoneinfo import ZoneInfo

from django.conf import settings
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.html import escape
from django.utils.safestring import mark_safe

from utils.emails import send_email_async

logger = logging.getLogger(__name__)

OTP_TEMPLATE = "emails/otp.html"
WELCOME_TEMPLATE = "emails/welcome.html"
PASSWORD_CHANGED_TEMPLATE = "emails/password_changed.html"

# Subjects live here, not in the templates, because they are also the
# <title> and the preview command needs them. The goat is deliberate.
WELCOME_SUBJECT = "Welcome to Goatza, {name} 🐐"
PASSWORD_CHANGED_SUBJECT = "Your Goatza password was changed"

# Users are in India and a security notice is only useful if the time in
# it is the time they remember. Project TIME_ZONE is UTC, so this is an
# explicit conversion rather than a localtime() call.
IST = ZoneInfo("Asia/Kolkata")

# Explicit rather than strftime("%b"): month abbreviations follow the
# process locale, and the mail must read the same on every host.
_MONTH_ABBR = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)

# ---------------------------------------------------------------------
# Per-email copy (reference files 01, 02, 03)
# ---------------------------------------------------------------------

# The fields below are rendered as HTML, not as text: they carry the exact
# entities (&rsquo;, &mdash;) the approved reference files use.
_HTML_COPY_FIELDS = ("preheader", "heading", "intro", "hint", "footer_reason")


def _copy(**fields):
    """Build a copy dict, marking the HTML fragments safe.

    These are static literals written in this file — never a request value — so
    turning autoescape off for them cannot inject anything. Everything that
    DOES come from outside (``name``, ``otp``) is passed separately at call
    time and stays escaped by the template.
    """
    return {
        key: mark_safe(value) if key in _HTML_COPY_FIELDS else value
        for key, value in fields.items()
    }


SIGNUP_OTP_COPY = _copy(
    subject="Your Goatza verification code",
    preheader="Your code is inside — valid for 10 minutes.",
    heading="Verify your email",
    intro=(
        "welcome to Goatza. Enter this code to finish creating your account:"
    ),
    hint="Didn&rsquo;t sign up for Goatza? You can safely ignore this email.",
    footer_reason=(
        "You&rsquo;re receiving this because this email was used to create a "
        "Goatza account."
    ),
)

LOGIN_OTP_COPY = _copy(
    subject="Your Goatza login code",
    preheader="Enter this code to log in and verify your email.",
    heading="Confirm it&rsquo;s you",
    intro=(
        "your email hasn&rsquo;t been verified yet. Enter this code to log in "
        "and verify it in one step:"
    ),
    hint=(
        "If you didn&rsquo;t try to log in, you can ignore this email "
        "&mdash; your account stays locked to your password."
    ),
    footer_reason=(
        "You&rsquo;re receiving this because a login was attempted on your "
        "Goatza account."
    ),
)

PASSWORD_RESET_OTP_COPY = _copy(
    subject="Reset your Goatza password",
    preheader="Your password reset code — valid for 10 minutes.",
    heading="Reset your password",
    intro=(
        "we received a request to reset your password. Use this code to "
        "continue:"
    ),
    hint=(
        "If you didn&rsquo;t request this, ignore this email &mdash; your "
        "password won&rsquo;t change."
    ),
    footer_reason=(
        "You&rsquo;re receiving this because a password reset was requested "
        "for your Goatza account."
    ),
)


# ---------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------

def format_ist_timestamp(dt):
    """Render `dt` as "4 Sep 2026 at 6:42 PM IST".

    No leading zero on the day or the hour — this is prose in a sentence, not a
    log line. A naive datetime is read as UTC, which is what every caller in
    this codebase means by one.
    """
    if timezone.is_naive(dt):
        dt = dt.replace(tzinfo=datetime_timezone.utc)

    local = dt.astimezone(IST)
    hour = local.hour % 12 or 12
    meridiem = "AM" if local.hour < 12 else "PM"

    return (
        f"{local.day} {_MONTH_ABBR[local.month - 1]} {local.year} "
        f"at {hour}:{local.minute:02d} {meridiem} IST"
    )


def shared_email_context():
    """Context every email template needs: links, logo, copyright year.

    Public because ``preview_emails`` renders the same templates outside a
    request and must inject exactly what a real send would.
    """
    frontend_base_url = (settings.FRONTEND_BASE_URL or "").rstrip("/")
    return {
        "frontend_base_url": frontend_base_url,
        "logo_url": settings.EMAIL_LOGO_URL,
        "year": timezone.localdate().year,
    }


def _send(subject, text_body, html_template, context, to_email):
    """Render `html_template` and hand the mail to the background sender.

    The whole body is guarded, not just the send: rendering is the part that
    can actually fail (bad template name, missing setting), and a caller in the
    middle of a signup has no useful way to react to a failed email.
    """
    try:
        html = render_to_string(
            html_template,
            {**context, "subject": subject, **shared_email_context()},
        )

        send_email_async(
            subject=subject,
            message=text_body,
            to_email=to_email,
            html_message=html,
        )
    except Exception as exc:
        logger.warning(
            f"transactional_emails | send failed | template={html_template} | "
            f"subject={subject!r} | {exc}"
        )
        return


# ---------------------------------------------------------------------
# OTP emails
# ---------------------------------------------------------------------

def send_signup_otp_email(*, name: str, email: str, otp: str) -> None:
    """Signup verification code — the very first mail an account receives."""
    _send(
        subject=SIGNUP_OTP_COPY["subject"],
        text_body=(
            f"Hi {name},\n\n"
            f"Welcome to Goatza. Enter this code to finish creating your "
            f"account:\n\n"
            f"{otp}\n\n"
            f"Valid for 10 minutes.\n\n"
            f"Didn't sign up for Goatza? You can safely ignore this email."
        ),
        html_template=OTP_TEMPLATE,
        context={**SIGNUP_OTP_COPY, "name": name, "otp": otp},
        to_email=email,
    )


def send_login_otp_email(*, name: str, email: str, otp: str) -> None:
    """Login code for an account whose email is still unverified."""
    _send(
        subject=LOGIN_OTP_COPY["subject"],
        text_body=(
            f"Hi {name},\n\n"
            f"Your email hasn't been verified yet. Enter this code to log in "
            f"and verify it in one step:\n\n"
            f"{otp}\n\n"
            f"Valid for 10 minutes.\n\n"
            f"If you didn't try to log in, you can ignore this email - your "
            f"account stays locked to your password."
        ),
        html_template=OTP_TEMPLATE,
        context={**LOGIN_OTP_COPY, "name": name, "otp": otp},
        to_email=email,
    )


def send_password_reset_otp_email(*, name: str, email: str, otp: str) -> None:
    """Forgot-password code."""
    _send(
        subject=PASSWORD_RESET_OTP_COPY["subject"],
        text_body=(
            f"Hi {name},\n\n"
            f"We received a request to reset your password. Use this code to "
            f"continue:\n\n"
            f"{otp}\n\n"
            f"Valid for 10 minutes.\n\n"
            f"If you didn't request this, ignore this email - your password "
            f"won't change."
        ),
        html_template=OTP_TEMPLATE,
        context={**PASSWORD_RESET_OTP_COPY, "name": name, "otp": otp},
        to_email=email,
    )


# ---------------------------------------------------------------------
# Lifecycle + security emails
# ---------------------------------------------------------------------

def send_welcome_email(*, name: str, email: str) -> None:
    """Sent once, the moment signup OTP verification succeeds."""
    profile_url = f"{shared_email_context()['frontend_base_url']}/profile"

    _send(
        subject=WELCOME_SUBJECT.format(name=name),
        text_body=(
            f"You're in, {name}.\n\n"
            f"Your email is verified. Goatza is where players get seen and "
            f"clubs find talent - here's how to get started:\n\n"
            f"1. Complete your profile - add your photo, position and playing "
            f"history so clubs know who you are.\n"
            f"2. Explore recruitments - open trials from verified clubs, "
            f"filtered for you.\n"
            f"3. Build your network - follow players and clubs, post your "
            f"highlights.\n\n"
            f"Complete your profile: {profile_url}"
        ),
        html_template=WELCOME_TEMPLATE,
        context={"name": name},
        to_email=email,
    )


def send_password_changed_email(
    *, name: str, email: str, changed_at=None
) -> None:
    """Security notice for a password that actually changed.

    Never sent on a failed attempt: an alert that fires when nothing happened
    is an alert people learn to ignore.
    """
    changed_at = format_ist_timestamp(changed_at or timezone.now())
    reset_url = (
        f"{shared_email_context()['frontend_base_url']}/auth/forgot-password"
    )

    _send(
        subject=PASSWORD_CHANGED_SUBJECT,
        text_body=(
            f"Hi {name},\n\n"
            f"The password for your Goatza account was changed on "
            f"{changed_at}.\n\n"
            f"If this was you, no action is needed.\n\n"
            f"Wasn't you? Reset your password immediately and contact support "
            f"- someone may have access to your account.\n\n"
            f"{reset_url}"
        ),
        html_template=PASSWORD_CHANGED_TEMPLATE,
        context={"name": name, "changed_at": changed_at},
        to_email=email,
    )


# ---------------------------------------------------------------------
# Recruitment emails
# ---------------------------------------------------------------------

APPLICATION_RECEIVED_TEMPLATE = "emails/application_received.html"
APPLICATION_STATUS_TEMPLATE = "emails/application_status.html"
NEW_APPLICANT_ALERT_TEMPLATE = "emails/new_applicant_alert.html"

# Only these four statuses are worth an email. `reviewing` is internal pipeline
# bookkeeping the player never asked about, `withdrawn` is their own action, and
# a move back to `applied` is a correction — mailing any of those trains people
# to ignore the ones that matter.
STATUS_EMAIL_COPY = {
    "selected": {
        "badge_label": "Selected",
        "badge_style": "background-color:#00B562;color:#ffffff;",
        "subject": "You're selected — {title} \U0001f389",
    },
    "shortlisted": {
        "badge_label": "Shortlisted",
        "badge_style": (
            "background-color:#F2FBF6;color:#007a3d;border:1.5px solid #00B562;"
        ),
        "subject": "You've been shortlisted — {title}",
    },
    "invited": {
        "badge_label": "Invited",
        "badge_style": (
            "background-color:#F2FBF6;color:#007a3d;border:1.5px solid #00B562;"
        ),
        "subject": "You're invited — {title}",
    },
    "rejected": {
        "badge_label": "Not selected",
        "badge_style": "background-color:#eef2ee;color:#556655;",
        "subject": "Update on your application — {title}",
    },
}

# Plain-text twins of the copy the status template branches to.
_STATUS_TEXT = {
    "selected": (
        "{org} selected you for: {title}",
        "The club may reach out with next steps - keep an eye on your Goatza "
        "messages.",
    ),
    "shortlisted": (
        "{org} shortlisted your application for: {title}",
        "Final decisions are coming - stay ready.",
    ),
    "invited": (
        "{org} invited you for: {title}",
        "Check the details and be ready.",
    ),
    "rejected": (
        "This time {org} went with other players for: {title}",
        "New recruitments open every week - your next trial is out there.",
    ),
}

_HTML_SEPARATOR = " &middot; "
_TEXT_SEPARATOR = " - "


def initials(name, limit=2):
    """First letters of up to `limit` words, uppercased — "TC", "AM".

    The avatar is a coloured box with text in it rather than an image, because
    email clients block images and a blocked avatar leaves a blank square.
    """
    words = (name or "").split()
    return "".join(word[0] for word in words[:limit]).upper()


def format_date(value):
    """A date as "4 Sep 2026" — the house style of format_ist_timestamp."""
    if value is None:
        return ""

    return f"{value.day} {_MONTH_ABBR[value.month - 1]} {value.year}"


def _present(segments):
    """Drop the segments that are absent, keeping a 0 that means something."""
    return [
        str(segment) for segment in segments
        if segment is not None and str(segment) != ""
    ]


def meta_tail(segments, leading=False):
    """Join the present segments with " &middot; ", escaping each one.

    Which segments exist varies per application — a recruitment may have no
    location, an application no position — and an absent one has to vanish
    rather than leave a dangling separator. Each value is escaped on its own and
    only the assembled result is marked safe, so separators stay markup and
    org-supplied text stays data.
    """
    present = [escape(segment) for segment in _present(segments)]
    if not present:
        return mark_safe("")

    joined = _HTML_SEPARATOR.join(present)
    return mark_safe(_HTML_SEPARATOR + joined if leading else joined)


def _text_tail(segments, leading=False):
    """The plain-text twin of meta_tail. No escaping — this is not markup."""
    present = _present(segments)
    if not present:
        return ""

    joined = _TEXT_SEPARATOR.join(present)
    return _TEXT_SEPARATOR + joined if leading else joined


def _profile_of(user):
    """The user's profile, or None — a user without one is not an error here."""
    return getattr(user, "profile", None)


def _player_name(application):
    """What to call the applicant.

    `shared_name` is what they typed on THIS application and what the org sees
    in its pipeline, so it wins; the profile name covers rows created without
    one.
    """
    if application.shared_name:
        return application.shared_name

    profile = _profile_of(application.applicant)
    return getattr(profile, "name", "") or application.applicant.username or ""


def _age_of(user):
    """Whole years from the profile birthdate, or None if it is not set."""
    profile = _profile_of(user)
    birthdate = getattr(profile, "birthdate", None)
    if not birthdate:
        return None

    today = timezone.localdate()
    return (
        today.year
        - birthdate.year
        - ((today.month, today.day) < (birthdate.month, birthdate.day))
    )


def _location_of(recruitment):
    """The recruitment's location on one line, or "" when it has none."""
    return recruitment.location_name or recruitment.city or ""


def _position_name(application):
    position = application.applied_position
    return getattr(position, "name", "") if position else ""


def _recruitment_card_context(application):
    """Values for emails/_recruitment_card.html, shared by two emails."""
    recruitment = application.recruitment
    organization = recruitment.organization

    return {
        "org_initials": initials(organization.name),
        "recruitment_title": recruitment.title,
        "org_name": organization.name,
        "card_meta_tail": meta_tail(
            [_position_name(application), _location_of(recruitment)],
            leading=True,
        ),
        "recruitment_id": recruitment.id,
    }


def send_application_received_email(*, application) -> None:
    """Confirm to the player that their application reached the club."""
    recipient = application.applicant.email
    if not recipient:
        return

    recruitment = application.recruitment
    context = _recruitment_card_context(application)
    player_name = _player_name(application)
    applied_date = format_date(application.applied_at)
    meta = [_position_name(application), _location_of(recruitment)]

    _send(
        subject=f"Application sent: {recruitment.title}",
        text_body=(
            f"Hi {player_name},\n\n"
            f"You applied to: {recruitment.title}\n"
            f"{context['org_name']}{_text_tail(meta, leading=True)}\n"
            f"Applied {applied_date}\n\n"
            f"The club is reviewing applications. We'll email you the moment "
            f"your status changes.\n\n"
            f"View application: "
            f"{shared_email_context()['frontend_base_url']}"
            f"/recruitments/{recruitment.id}"
        ),
        html_template=APPLICATION_RECEIVED_TEMPLATE,
        context={
            **context,
            "player_name": player_name,
            "applied_date": applied_date,
        },
        to_email=recipient,
    )


def send_application_status_email(*, application, to_status) -> None:
    """Tell the player their application moved — for the four states worth it.

    Any other status returns without sending, which is what makes this safe to
    call unconditionally from the status-change loop.
    """
    copy = STATUS_EMAIL_COPY.get(to_status)
    if copy is None:
        return

    recipient = application.applicant.email
    if not recipient:
        return

    recruitment = application.recruitment
    context = _recruitment_card_context(application)
    player_name = _player_name(application)
    base_url = shared_email_context()["frontend_base_url"]

    headline, closing = _STATUS_TEXT[to_status]
    button_url = (
        f"{base_url}/recruitments"
        if to_status == "rejected"
        else f"{base_url}/recruitments/{recruitment.id}"
    )

    _send(
        subject=copy["subject"].format(title=recruitment.title),
        text_body=(
            f"Hi {player_name},\n\n"
            f"{headline.format(org=context['org_name'], title=recruitment.title)}"
            f"\n\n"
            f"{closing}\n\n"
            f"{button_url}"
        ),
        html_template=APPLICATION_STATUS_TEMPLATE,
        context={
            **context,
            "to_status": to_status,
            "player_name": player_name,
            "badge_label": copy["badge_label"],
            # A literal from the table above, never a request value.
            "badge_style": mark_safe(copy["badge_style"]),
        },
        to_email=recipient,
    )


def new_applicant_alert_recipients(organization):
    """Emails of the members who actually triage applications.

    Owners and admins only: coaches and staff read a recruitment, they do not
    run its pipeline. Deduplicated and order-stable, and inactive or
    email-less members are dropped rather than handed to the transport.
    """
    # Imported here rather than at module scope: this module is pulled in by
    # accounts views very early, and it has no other reason to depend on the
    # organization app.
    from organization.models import OrganizationMember

    emails = (
        organization.members
        .filter(
            role__in=[
                OrganizationMember.Role.OWNER,
                OrganizationMember.Role.ADMIN,
            ],
            user__is_active=True,
        )
        .exclude(user__email="")
        .exclude(user__email__isnull=True)
        .values_list("user__email", flat=True)
    )

    # dict.fromkeys rather than set(): recipients should not reshuffle between
    # sends, which makes a bug report about "who got it" answerable.
    return list(dict.fromkeys(emails))


def send_new_applicant_alert_email(
    *, recruitment, latest_application, new_count, total_count
) -> None:
    """Alert the org's owners and admins, in ONE mail, about new applicants.

    `new_count` is how many arrived since the last alert. Above 1 means the
    tiered throttle held some back and this mail stands for all of them — the
    template switches to its rollup wording off exactly that.
    """
    organization = recruitment.organization
    recipients = new_applicant_alert_recipients(organization)
    if not recipients:
        return

    applicant = latest_application.applicant
    player_name = _player_name(latest_application)
    is_rollup = new_count > 1
    more_count = new_count - 1

    meta = [
        _position_name(latest_application),
        _age_of(applicant),
        _location_of(recruitment),
    ]

    subject = (
        f"{new_count} new applicants — {recruitment.title}"
        if is_rollup
        else f"New applicant: {player_name} — {recruitment.title}"
    )
    lead = (
        f"Latest to apply to {recruitment.title}:"
        if is_rollup
        else f"A player just applied to {recruitment.title}:"
    )
    rollup_line = (
        f"...and {more_count} more since your last alert.\n\n"
        if is_rollup
        else ""
    )
    admin_url = (
        f"{shared_email_context()['frontend_base_url']}"
        f"/organization/admin/{organization.id}"
    )

    _send(
        subject=subject,
        text_body=(
            f"{lead}\n\n"
            f"{player_name} @{applicant.username}\n"
            f"{_text_tail(meta)}\n\n"
            f"{rollup_line}"
            f"{total_count} applications so far for this recruitment.\n\n"
            f"Review: {admin_url}"
        ),
        html_template=NEW_APPLICANT_ALERT_TEMPLATE,
        context={
            "is_rollup": is_rollup,
            "new_count": new_count,
            "more_count": more_count,
            "total_count": total_count,
            "organization_id": organization.id,
            "org_name": organization.name,
            "recruitment_title": recruitment.title,
            "applicant_initials": initials(player_name),
            "player_name": player_name,
            "player_username": applicant.username,
            "applicant_meta": meta_tail(meta),
        },
        # One call, one mail, every owner and admin on it.
        to_email=recipients,
    )
