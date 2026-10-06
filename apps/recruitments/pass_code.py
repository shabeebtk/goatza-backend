# recruitments/pass_code.py
"""
The booking reference on a player's trial pass.

IT IS NOT A CHECK-IN CODE. Nothing scans it and nothing checks anyone in
against it — the app does not keep a register. It is what a player shows if an
organiser asks to confirm they registered: a short, quotable reference that
ties a person standing at a ground to a row in the org's applicant list.

ONE HOME FOR THE MINTING. There are three paths that move an application into
``trial_confirmed`` today — ``change_status``, the auto-confirm branch of
``apply``, and its reapply branch. All of them call ``mint_for`` rather than
generating a code themselves, so a fourth path added later cannot forget: the
rule lives with the status write, not beside it.

THE ALPHABET IS STILL THE POINT. The code is read aloud off a phone screen in
daylight and typed or written down by a person, so it drops every character
pair that gets misread: no 0/O, no 1/I/L. What is left is 32 symbols, and 8 of
them is ~2^40 — vastly more than a trial with a few hundred players needs,
which is what makes a collision retry a formality rather than a hot path.

``secrets.choice``, never ``random``: a reference somebody can guess is a
reference that identifies nobody, and ``random`` is seeded predictably enough
to enumerate.
"""

import secrets

# 0/O and 1/I/L are gone. Everything else is a character somebody can read off
# a screen and type without hesitating.
ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"

CODE_LENGTH = 8
GROUP_SIZE = 4

# Attempts before giving up. With a 32-symbol alphabet and 8 characters,
# reaching the fifth draw means something is wrong with the alphabet or the
# uniqueness scope, not that the trial got unlucky.
MAX_ATTEMPTS = 5


def generate_code():
    """``"4KX9-72BQ"`` — grouped, because an 8-character run is misread."""
    raw = "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))
    return f"{raw[:GROUP_SIZE]}-{raw[GROUP_SIZE:]}"


def normalize_code(value):
    """
    What somebody typed, as the stored form.

    Case-insensitive and hyphen-optional, because a person reading a code back
    will write ``4kx972bq`` and be right. Anything that is not in the alphabet
    is dropped rather than rejected — a stray space or a second hyphen is a
    typo, not an attack, and a lookup will simply miss if the rest is wrong.
    """
    if not value:
        return ""

    cleaned = "".join(
        character
        for character in str(value).upper()
        if character in ALPHABET
    )
    if len(cleaned) != CODE_LENGTH:
        return ""

    return f"{cleaned[:GROUP_SIZE]}-{cleaned[GROUP_SIZE:]}"


def mint_for(application, *, save=True):
    """
    Give ``application`` a pass code, if it does not have one.

    IDEMPOTENT, and that is the whole contract. An application that already
    has a code keeps it — moving out of ``trial_confirmed`` and back must not
    issue a second one, because the player may already have screenshotted the
    first and a re-issue would send them to a desk that cannot find them.

    Uniqueness is per RECRUITMENT (the partial constraint on the model), which
    is also the only scope a code is ever read in. The retry is here rather
    than trusting one draw: the constraint would otherwise turn a
    one-in-a-trillion collision into a 500 on a confirmation.

    ``save=False`` sets the attribute and leaves persisting to the caller —
    ``change_status`` writes the whole batch in one ``bulk_update``.
    """
    if application.pass_code:
        return application.pass_code

    from apps.recruitments.models import RecruitmentApplication

    for _ in range(MAX_ATTEMPTS):
        candidate = generate_code()

        taken = (
            RecruitmentApplication.objects
            .filter(
                recruitment_id=application.recruitment_id,
                pass_code=candidate,
            )
            .exists()
        )
        if taken:
            continue

        application.pass_code = candidate
        if save:
            application.save(update_fields=["pass_code"])
        return candidate

    raise RuntimeError(
        "Could not mint a unique pass code for recruitment "
        f"{application.recruitment_id} after {MAX_ATTEMPTS} attempts."
    )
