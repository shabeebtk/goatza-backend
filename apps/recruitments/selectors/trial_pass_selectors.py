# recruitments/selectors/trial_pass_selectors.py
"""
The trial pass: everything a confirmed player shows at the gate.

ONE PAYLOAD, ASSEMBLED ONCE. The pass is read on a page, rendered into an
image and shared to a parent, and all three have to agree — so the shape is
built here rather than three times over.

IT IS PERSONAL. Unlike a recruitment, which is a public posting, a pass names
one player, their age group and their fee status. The view answers 404 for
anybody but the applicant — never 403, which would confirm the id is real.

``pass_code`` is the booking reference the player shows if an organiser asks
to confirm they registered. It is minted on confirmation, so a pass that
exists at all carries one — nothing scans it and nothing checks anybody in
against it.
"""

from datetime import time

from apps.recruitments.serializers.recruitment_list_serializers import (
    trial_session_payload,
)


def _profile_photo(user):
    profile = getattr(user, "profile", None)
    return getattr(profile, "profile_photo", "") or ""


def _org_logo(organization):
    profile = getattr(organization, "profile", None)
    return getattr(profile, "logo", "") or ""


def pass_sessions(application):
    """
    The date(s) on the pass.

    choose_one: the ONE they picked, because that is the only day they are
    expected and a pass listing four cities is a pass nobody can read at a
    gate. Every other mode: every non-cancelled date, because they are
    expected at all of them.

    Cancelled dates are left out entirely here, unlike the posting — a pass
    is an instruction, not a history of the trial's scheduling.
    """
    recruitment = application.recruitment

    if (
        recruitment.session_mode == recruitment.SessionMode.CHOOSE_ONE
        and application.session is not None
        and not application.session.is_cancelled
    ):
        sessions = [application.session]
    else:
        sessions = [
            session
            for session in recruitment.sessions.all()
            if not session.is_cancelled
        ]
        # A date with no time is the end-of-day sentinel everywhere else in
        # this codebase, so it sorts after a timed one on the same day.
        sessions.sort(
            key=lambda s: (s.date, s.start_time or time(23, 59), s.display_order)
        )

    return [
        trial_session_payload(session, recruitment) for session in sessions
    ]


def build_trial_pass(application):
    """The whole pass, as the page and the image both read it."""
    recruitment = application.recruitment
    organization = recruitment.organization
    applicant = application.applicant
    group = application.age_category

    return {
        "application_id": str(application.id),
        "status": application.status,

        "organization": {
            "id": str(organization.id),
            "name": organization.name,
            "username": organization.username,
            "logo": _org_logo(organization),
            "is_verified": organization.is_verified,
        },
        "recruitment": {
            "id": str(recruitment.id),
            "title": recruitment.title,
        },

        "player": {
            # shared_name is what they put on THIS application and what the
            # org will read off its own list at the gate, so it wins over the
            # profile name — the two names have to match.
            "name": application.shared_name
            or getattr(getattr(applicant, "profile", None), "name", "")
            or applicant.username,
            "username": applicant.username,
            "photo": _profile_photo(applicant),
        },

        "age_group": None if group is None else {
            "id": str(group.id),
            "title": group.title,
            "min_birth_year": group.min_birth_year,
            "max_birth_year": group.max_birth_year,
            "reporting_time": group.reporting_time,
        },

        "sessions": pass_sessions(application),

        "bring": [
            {
                "id": str(requirement.id),
                "title": requirement.title,
                "is_mandatory": requirement.is_mandatory,
            }
            for requirement in recruitment.requirements.all()
        ],

        "fee": {
            "is_paid_trial": recruitment.is_paid,
            "amount": str(recruitment.fee_amount) if recruitment.fee_amount else None,
            "currency": recruitment.fee_currency,
            "note": recruitment.payment_note,
            # As the ORG recorded it. Information, never a gate: an unpaid
            # player still has a pass and is still expected.
            "fee_paid": application.fee_paid,
        },

        # The booking reference. Minted on confirmation, and a pass is only
        # ever built for a confirmed application, so it is always here.
        "pass_code": application.pass_code,
    }
