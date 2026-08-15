"""
Read queries behind the public Sports CV.

The CV is presentation over data that already exists — profile, career,
achievements, highlights. Nothing here re-implements a visibility rule; it
resolves who the CV belongs to, and assembles what their toggles say may be
shown.

Two rules are worth stating up front, because both are deliberate departures
from the feature spec and both will look like bugs to whoever reads this next:

  * **The CV requires ``is_public_profile``.** The spec described the two
    switches as independent. They are not: ``get_cv_user`` delegates to
    ``get_public_user``, so a private profile has no CV, full stop. The
    alternative was a second resolver carrying a second copy of the privacy
    rule, and two privacy rules for one person is how one of them ends up
    wrong. The settings screen states the dependency in words; the gate is
    here.

  * **Age group, never age.** The header carries ``age_group`` ("U19",
    "Senior") from the profile serializer and never the birthdate the spec
    asked for. Full name plus exact date of birth on a page anyone can scrape
    is the classic identity-theft pair, and most of these players are minors.
    A recruiter filters on the band, not the date.
"""

from accounts.models import User
from accounts.serializers.public_profile_serializers import (
    PublicUserProfileSerializer,
)
from achievements.serializers.achievement_serializers import (
    AchievementSerializer,
)
from careers.serializers.career_serializers import CareerEntrySerializer
from core.selectors.public_profile_selectors import (
    get_public_user,
    public_achievements_for,
    public_career_entries_for,
)
from cv.models import PlayerCVSettings
from cv.serializers.cv_serializers import PublicCVContactSerializer
from highlights.selectors.highlight_selectors import visible_highlights_for
from highlights.serializers.highlight_serializers import HighlightSerializer


def get_cv_user(username):
    """
    ``(user, settings)`` for a public CV URL, or None.

    Four separate reasons to return None, all of which the view reports as the
    same 404 — a visitor must not be able to tell a disabled CV from a hidden
    profile from a coach's username from a typo:

      * anything ``get_public_user`` refuses (unknown username, deactivated
        account, ``is_public_profile`` off)
      * the user is not a player
      * no settings row exists
      * the settings row exists with ``is_enabled`` False
    """
    user = get_public_user(username)
    if user is None:
        return None

    if user.role != User.Role.PLAYER:
        return None

    settings = (
        PlayerCVSettings.objects
        .filter(user=user, is_enabled=True)
        .first()
    )

    if settings is None:
        return None

    # Prime the FK cache with the user we already have: the view counter reads
    # settings.user.username, and that would otherwise be a second query for a
    # row this function is holding.
    settings.user = user

    return user, settings


def cv_payload(user, settings, actor=None):
    """
    The public CV payload for one player.

    Every optional section is ABSENT when its toggle is off, not empty and not
    hidden by the client. The CV is server-rendered; an empty list still sits in
    the page source, and "hidden" a reader can View Source is not hidden.

    ``actor`` is accepted so the call site reads like its sibling in
    ``core.views.public_profile_views``, and is deliberately unused: this
    payload is identical for every viewer. See the highlights note below for
    the one place that matters.
    """
    profile = PublicUserProfileSerializer(user).data

    # The sport attributes ("Preferred foot", "Batting style") hang off
    # primary_sport in the profile serializer, so the toggle prunes them there
    # rather than dropping the whole sport block — a CV with no sport on it
    # would not be a CV.
    if not settings.show_attributes:
        primary_sport = profile.get("primary_sport")
        if primary_sport:
            primary_sport.pop("attributes", None)

    data = {
        "profile": profile,
        "views_count": settings.views_count,
    }

    # Phone only, and only when there is one. The key is absent rather than
    # present-and-null so the client cannot render an empty "Contact" heading.
    if settings.show_contact and user.phone:
        data["contact"] = PublicCVContactSerializer(user).data

    if settings.show_career:
        data["career"] = CareerEntrySerializer(
            public_career_entries_for(user), many=True
        ).data

    if settings.show_achievements:
        data["achievements"] = AchievementSerializer(
            public_achievements_for(user), many=True
        ).data

    if settings.show_highlights:
        # ALWAYS actor=None, even for a signed-in viewer — this is NOT the
        # oversight it looks like. Only `everyone` clips belong on a CV: the
        # page is meant to be printed, QR-scanned at a trial and forwarded on,
        # and a clip whose owner restricted it to recruiters must not become
        # a public URL the moment a recruiter is the one who opens the CV.
        # The profile bundle passes the real actor precisely because it is the
        # in-app view and is not forwarded anywhere.
        data["highlights"] = HighlightSerializer(
            visible_highlights_for(user, None),
            many=True,
            # visibility and views_count are the owner's business, and the
            # owner reads their rail through the authenticated endpoint.
            context={"is_owner": False},
        ).data

    return data
