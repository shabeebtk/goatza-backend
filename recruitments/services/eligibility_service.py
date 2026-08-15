# recruitments/services/eligibility_service.py
"""
Player-fit eligibility (spec §2) — for DISPLAY and RANKING only.

Two ideas that look alike and are enforced completely differently:

  HARD STATE   status / deadline / max-applications cap. Enforced server-side by
               ``Recruitment.is_accepting_applications`` and re-checked under a
               row lock in ApplicationService.apply. Nothing here touches it.

  PLAYER FIT   age band, gender. Evaluated here, and used for exactly two
               things: sinking the row in the ranking (×0.05) and rendering an
               informational badge. It never gates the Apply button, never
               filters a list on its own, and never reaches the apply endpoint.
               Goatza displays eligibility; the venue verifies it. That stance
               is stated at the top of the web client's `eligibility.ts` and
               this module is its server-side twin, not its enforcement arm.

Missing data is never a disqualifier. §3 says missing coordinates score "+5
neutral, never punish missing data"; the same rule applies to every check here.
No birthdate → the age check PASSES. No gender → the gender check PASSES. Young
players are exactly the demographic that leaves DOB blank, and burying their
feed for it would make this ranking worse than the newest-first list it
replaces.

The one place a verdict does filter is ``age_eligible=true`` on the "All" tab,
because there the user asked for it in so many words.
"""

from dataclasses import dataclass, field

from django.utils import timezone

from recruitments.models import Recruitment

# Machine keys for the failing checks. The badge is a display string; these are
# what callers branch on.
REASON_AGE = "age"
REASON_GENDER = "gender"
REASON_DEADLINE = "deadline"

# Badge precedence when more than one check fails. Deadline first: it is the
# only one the player cannot argue with, and the only one that also stops the
# apply endpoint, so leading with anything else would read as the reason.
_BADGE_PRIORITY = (REASON_DEADLINE, REASON_AGE, REASON_GENDER)

# Age badges name the groups the recruitment is open to. Past this many, the
# list stops being a badge and starts being the card's age chip.
_MAX_BADGE_AGE_TITLES = 3


@dataclass(frozen=True)
class EligibilityVerdict:
    """Why a recruitment does or does not fit this player."""

    is_eligible: bool
    reasons: list[str] = field(default_factory=list)
    badge: str | None = None


ELIGIBLE = EligibilityVerdict(is_eligible=True, reasons=[], badge=None)


def evaluate(recruitment, context, now=None):
    """
    Verdict for one recruitment against a resolved player context.

    Pure: no ORM access beyond reading already-prefetched relations, so the
    caller controls the query count and the weights stay unit-testable. Pass
    ``now`` to pin the deadline comparison in tests.

    ``context`` is a PlayerContext (see
    recruitments.selectors.player_context_selectors); anything exposing
    ``birth_year`` and ``gender`` works.
    """
    now = now or timezone.now()

    badges = {}

    age_badge = _age_badge(recruitment, context.birth_year)
    if age_badge:
        badges[REASON_AGE] = age_badge

    gender_badge = _gender_badge(recruitment, context.gender)
    if gender_badge:
        badges[REASON_GENDER] = gender_badge

    deadline_badge = _deadline_badge(recruitment, now)
    if deadline_badge:
        badges[REASON_DEADLINE] = deadline_badge

    if not badges:
        return ELIGIBLE

    reasons = [key for key in _BADGE_PRIORITY if key in badges]
    return EligibilityVerdict(
        is_eligible=False,
        reasons=reasons,
        badge=badges[reasons[0]],
    )


# ---------------------------------------------------------------- #
# CHECKS — each returns a badge string on failure, None on pass
# ---------------------------------------------------------------- #

def _age_badge(recruitment, birth_year):
    """
    Passes when the player's birth year falls inside at least ONE age
    category's band.

    Deliberately the same reading of a band as the ``birth_year`` filter in
    RecruitmentSelector.list_recruitments: a null bound never excludes, and both
    bounds have to be satisfied by the SAME category row (a min from one group
    and a max from another is not a match). Keep the two in step — a second,
    subtly different interpretation is how "the filter found it but the badge
    says no" bugs get made.
    """
    if birth_year is None:
        # Unknown, not ineligible.
        return None

    categories = list(recruitment.age_categories.all())
    if not categories:
        # An empty list already means "open to all ages" (see the
        # RecruitmentAgeCategory docstring) — there is no separate flag.
        return None

    for category in categories:
        if birth_year_in_category(category, birth_year):
            return None

    return _age_badge_text(categories)


def birth_year_in_category(category, birth_year):
    """One category's band, matching the selector's SQL filter exactly."""
    if (
        category.min_birth_year is not None
        and category.min_birth_year > birth_year
    ):
        return False
    if (
        category.max_birth_year is not None
        and category.max_birth_year < birth_year
    ):
        return False
    return True


def _age_badge_text(categories):
    """"U-17 only" · "U-17 / U-19 only" — the groups it IS open to."""
    titles = [c.title.strip() for c in categories if c.title.strip()]
    if not titles:
        return "Age group restricted"

    shown = titles[:_MAX_BADGE_AGE_TITLES]
    text = " / ".join(shown)
    if len(titles) > len(shown):
        text += " …"
    return f"{text} only"


def _gender_badge(recruitment, gender):
    """
    Passes when the recruitment is open to everyone, or the player's gender
    matches. A blank recruitment gender means the same thing as "all" — the
    field is optional on the create form and most postings leave it empty.
    """
    required = recruitment.gender
    if not required or required == Recruitment.Gender.ALL:
        return None

    if not gender:
        # Profile gender unset — unknown, not a mismatch.
        return None

    if gender == required:
        return None

    label = dict(Recruitment.Gender.choices).get(required, required)
    return f"Open to {label.lower()} only"


def _deadline_badge(recruitment, now):
    """Passes when there is no deadline, or it has not passed yet."""
    deadline = recruitment.application_deadline
    if deadline and deadline < now:
        return "Applications closed"
    return None
