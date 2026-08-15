# recruitments/selectors/player_context_selectors.py
"""
Everything the recruitment ranker needs to know about the viewer, resolved
ONCE per request (spec §4).

The whole point of this module is the "once". Every signal in §3 — sport,
positions, distance, follows — is a property of the viewer, not of the
recruitment, so resolving it per candidate row would turn a few hundred cheap
Python comparisons into a few hundred round trips.
"""

from dataclasses import dataclass

from sports.models import UserSport, UserSportPosition
from connections.services.follow_services import FollowService
from feed.services.explore_services import ExploreService

# Profile fields the score actually reads. Named individually (not as one
# generic "complete your profile" nudge) because the client tells the player
# WHICH one is missing, and an honest prompt has to know.
FIELD_SPORT = "sport"
FIELD_POSITIONS = "positions"
FIELD_BIRTHDATE = "birthdate"
FIELD_LOCATION = "location"


@dataclass(frozen=True)
class PlayerContext:
    """
    A resolved viewer. Also valid — and deliberately so — for an org actor or
    an anonymous caller, where every personalized field is simply empty and the
    scorer degrades to the non-personalized signals on its own.
    """

    user_id: str | None = None
    primary_sport_id: str | None = None
    sport_ids: frozenset = frozenset()
    position_ids: frozenset = frozenset()
    birth_year: int | None = None
    gender: str = ""
    latitude: float | None = None
    longitude: float | None = None
    followed_org_ids: frozenset = frozenset()

    @property
    def center(self):
        """(lat, lng) for the distance annotation, or None when unlocatable."""
        if self.latitude is None or self.longitude is None:
            return None
        return (self.latitude, self.longitude)

    @property
    def is_personalized(self):
        """
        Whether a match score means anything for this viewer.

        Sport is the load-bearing signal: it is worth +40 of a ~100 point scale,
        and without it every candidate scores within a few points of every
        other, so "ranked by match" would be a lie. The endpoint still answers —
        ordered by freshness / deadline / distance — and the client shows the
        profile-completion prompt.
        """
        return bool(self.sport_ids)

    @property
    def missing_fields(self):
        """Which of the four scored profile fields this viewer hasn't filled."""
        missing = []
        if not self.sport_ids:
            missing.append(FIELD_SPORT)
        if not self.position_ids:
            missing.append(FIELD_POSITIONS)
        if self.birth_year is None:
            missing.append(FIELD_BIRTHDATE)
        if self.center is None:
            missing.append(FIELD_LOCATION)
        return missing


class PlayerContextSelector:

    @staticmethod
    def resolve(actor):
        """
        Four queries for a player (sports, positions, and the two follow reads
        inside FollowService), two for an org actor, zero for anonymous.

        An org actor keeps its location so "near you" still works from the club's
        primary venue, and keeps its follow graph — both are real signals. It has
        no sports/positions/age/gender, which is what makes it non-personalized.
        """
        if actor is None:
            return PlayerContext()

        # Reused verbatim: the same "profile lat/lng for a user, primary
        # OrganizationLocation for an org" rule Explore already resolves.
        location = ExploreService.resolve_location(actor)
        latitude, longitude = location if location else (None, None)

        followed_org_ids = frozenset(
            FollowService.get_following_ids(actor)["org_ids"]
        )

        if not actor.is_user:
            return PlayerContext(
                latitude=latitude,
                longitude=longitude,
                followed_org_ids=followed_org_ids,
            )

        user = actor.user

        primary_sport_id = None
        sport_ids = set()
        for sport_id, is_primary in UserSport.objects.filter(
            user=user
        ).values_list("sport_id", "is_primary"):
            sport_ids.add(sport_id)
            if is_primary:
                primary_sport_id = sport_id

        position_ids = frozenset(
            UserSportPosition.objects
            .filter(user=user)
            .values_list("position_id", flat=True)
        )

        profile = getattr(user, "profile", None)
        birthdate = getattr(profile, "birthdate", None)

        return PlayerContext(
            user_id=user.id,
            primary_sport_id=primary_sport_id,
            sport_ids=frozenset(sport_ids),
            position_ids=position_ids,
            birth_year=birthdate.year if birthdate else None,
            gender=getattr(profile, "gender", "") or "",
            latitude=latitude,
            longitude=longitude,
            followed_org_ids=followed_org_ids,
        )
