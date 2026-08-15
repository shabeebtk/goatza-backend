# recruitments/services/match_score_service.py
"""
The recruitment match score (spec §3).

An additive, ~0–100 score over fields that already exist on both sides. This is
structured matching, not ML: the player's sport, positions, birth year, gender
and coordinates against the recruitment's. Arithmetic, and cheap enough to run
over every active row per request (§1, §7 phase 1).

Distance arrives pre-annotated from SQL (``distance_km``); everything else is
compared in Python over the candidate list, which is what keeps the weights
below unit-testable against §3's worked example.

WEIGHTS ARE DATA. §8 tunes them from logged outcomes — that is only possible if
there is one place to change. Nothing in this module hard-codes a number that
isn't in MATCH_WEIGHTS.
"""

import math
from dataclasses import dataclass
from datetime import timedelta

from recruitments.services import eligibility_service

# ---------------------------------------------------------------- #
# WEIGHTS (§3) — the single tuning surface
# ---------------------------------------------------------------- #

MATCH_WEIGHTS = {
    # Sport. The dominant signal: a basketball trial is not a near-miss for a
    # footballer, it is a different sport.
    "sport_primary": 40,
    "sport_other": 20,
    "sport_none": 0,

    # Position. "Neutral" is 8, not 0, and it is paid in BOTH unknown
    # directions — the recruitment listed no positions, or the player listed
    # none. Never punish missing data (§3): an unstated position is not a
    # mismatch. It is still worth less than a real overlap, so the profile
    # prompt's "this weakens your matches" stays honest.
    "position_overlap": 15,
    "position_unspecified": 8,
    "position_mismatch": 0,

    # Distance bands (km). Unknown is +5 — mid-band, per §3's explicit
    # "never punish missing data" on coordinates.
    "distance_under_10": 15,
    "distance_under_25": 12,
    "distance_under_50": 8,
    "distance_under_100": 4,
    "distance_beyond": 0,
    "distance_unknown": 5,

    # Deadline urgency — surfaces "last chance" organically.
    "deadline_within_3_days": 12,
    "deadline_within_7_days": 8,
    "deadline_none": 0,

    # Existing follow graph.
    "followed_org": 10,

    # Freshness.
    "published_within_48h": 8,
    "published_within_7_days": 4,
    "published_older": 0,

    # Small trust nudge from the existing verified flag.
    "verified_org": 3,

    # Rank down, never hide (§2). An ineligible recruitment keeps its badge and
    # its place in the list — at the bottom of it. Players share trials with
    # siblings and teammates, and a visible reason builds trust in the ranking.
    "ineligible_multiplier": 0.05,
}

# (upper bound in km, weight key) — first band whose bound the distance is
# under wins. Ordered near → far.
DISTANCE_BANDS = (
    (10, "distance_under_10"),
    (25, "distance_under_25"),
    (50, "distance_under_50"),
    (100, "distance_under_100"),
)

# (upper bound in days, weight key) — soonest first.
DEADLINE_BANDS = (
    (3, "deadline_within_3_days"),
    (7, "deadline_within_7_days"),
)

# (age in hours, weight key) — freshest first.
FRESHNESS_BANDS = (
    (48, "published_within_48h"),
    (24 * 7, "published_within_7_days"),
)

# Chip vocabulary for §5 ("Your sport · Striker · 8 km · Closes in 5 days").
SPORT_PRIMARY = "primary"
SPORT_OTHER = "other"
SPORT_NONE = "none"


@dataclass(frozen=True)
class MatchResult:
    """One recruitment scored for one viewer, plus the chip data behind it."""

    score: float
    raw_score: float
    verdict: eligibility_service.EligibilityVerdict

    # §5: the card shows the REASONS, never the number. A score invites
    # argument; "Your sport · Striker · 8 km" builds trust.
    sport_match: str = SPORT_NONE
    position_match: bool | None = None
    # The positions that actually overlapped, so the chip can say "Striker"
    # instead of guessing from the recruitment's list — which would be wrong
    # the moment a posting names three positions and the player plays one.
    matched_positions: tuple = ()
    distance_km: float | None = None
    days_to_deadline: int | None = None

    @property
    def is_eligible(self):
        return self.verdict.is_eligible

    @property
    def badge(self):
        return self.verdict.badge


class MatchScoreService:

    @classmethod
    def score(cls, recruitment, context, now):
        """
        Score one recruitment. ``recruitment`` must have its ``positions`` and
        ``age_categories`` prefetched and (when the viewer is locatable) a
        ``distance_km`` annotation; ``now`` is passed in so a whole page shares
        one clock.
        """
        verdict = eligibility_service.evaluate(recruitment, context, now=now)

        sport_match = cls._sport_match(recruitment, context)
        position_match, matched_positions = cls._position_match(
            recruitment, context
        )
        distance_km = getattr(recruitment, "distance_km", None)
        days_to_deadline = cls._days_to_deadline(recruitment, now)

        raw = (
            MATCH_WEIGHTS[f"sport_{sport_match}"]
            + cls._position_points(position_match)
            + cls._distance_points(distance_km)
            + cls._deadline_points(recruitment, now)
            + cls._follow_points(recruitment, context)
            + cls._freshness_points(recruitment, now)
            + cls._verified_points(recruitment)
        )

        final = raw
        if not verdict.is_eligible:
            final = raw * MATCH_WEIGHTS["ineligible_multiplier"]

        return MatchResult(
            score=round(final, 2),
            raw_score=round(raw, 2),
            verdict=verdict,
            sport_match=sport_match,
            position_match=position_match,
            matched_positions=matched_positions,
            distance_km=(
                round(distance_km, 1) if distance_km is not None else None
            ),
            days_to_deadline=days_to_deadline,
        )

    @classmethod
    def score_all(cls, recruitments, context, now):
        """
        Score a candidate list, returning [(recruitment, MatchResult), ...] in
        the order given. Callers sort; this stays a pure map.
        """
        return [(r, cls.score(r, context, now)) for r in recruitments]

    # ------------------------------------------------------------ #
    # SIGNALS
    # ------------------------------------------------------------ #

    @staticmethod
    def _sport_match(recruitment, context):
        """
        primary / other / none.

        A viewer with no sports at all scores "none" on every candidate, so the
        signal cancels out and the ranking degrades to the remaining ones by
        itself — no special case, and no fake bonus that would only add noise.
        """
        sport_id = recruitment.sport_id
        if context.primary_sport_id and sport_id == context.primary_sport_id:
            return SPORT_PRIMARY
        if sport_id in context.sport_ids:
            return SPORT_OTHER
        return SPORT_NONE

    @staticmethod
    def _position_match(recruitment, context):
        """
        (verdict, matched position names).

        Verdict is True on overlap, False on a real mismatch, and None when
        either side left positions unstated — the "neutral" case, not a
        mismatch.
        """
        rows = list(recruitment.positions.all())
        if not rows or not context.position_ids:
            return None, ()

        overlap = tuple(
            row.position.name
            for row in rows
            if row.position_id in context.position_ids
        )
        return bool(overlap), overlap

    @staticmethod
    def _position_points(position_match):
        if position_match is None:
            return MATCH_WEIGHTS["position_unspecified"]
        if position_match:
            return MATCH_WEIGHTS["position_overlap"]
        return MATCH_WEIGHTS["position_mismatch"]

    @staticmethod
    def _distance_points(distance_km):
        if distance_km is None:
            return MATCH_WEIGHTS["distance_unknown"]
        for bound, key in DISTANCE_BANDS:
            if distance_km < bound:
                return MATCH_WEIGHTS[key]
        return MATCH_WEIGHTS["distance_beyond"]

    @staticmethod
    def _days_to_deadline(recruitment, now):
        """
        Whole days left, rounded UP so "23 hours" reads as "closes in 1 day"
        rather than "0". Negative once the deadline has passed; None when the
        recruitment has no deadline at all.

        The past is floored, not ceiled: ``ceil`` rounds -0.125 days (a
        deadline that lapsed three hours ago) back to 0, and the card then
        announced "Closes today" about a posting whose own detail page said
        "Applications closed". Anything already over is at least -1.
        """
        deadline = recruitment.application_deadline
        if not deadline:
            return None

        seconds = (deadline - now).total_seconds()
        if seconds <= 0:
            return min(-1, math.floor(seconds / 86400))
        return math.ceil(seconds / 86400)

    @classmethod
    def _deadline_points(cls, recruitment, now):
        deadline = recruitment.application_deadline
        if not deadline or deadline < now:
            # A passed deadline earns no urgency bonus — it is not urgent, it
            # is over, and eligibility has already sunk the row.
            return MATCH_WEIGHTS["deadline_none"]

        for days, key in DEADLINE_BANDS:
            if deadline <= now + timedelta(days=days):
                return MATCH_WEIGHTS[key]
        return MATCH_WEIGHTS["deadline_none"]

    @staticmethod
    def _follow_points(recruitment, context):
        if recruitment.organization_id in context.followed_org_ids:
            return MATCH_WEIGHTS["followed_org"]
        return 0

    @staticmethod
    def _freshness_points(recruitment, now):
        published_at = recruitment.published_at
        if not published_at:
            return MATCH_WEIGHTS["published_older"]

        for hours, key in FRESHNESS_BANDS:
            if published_at >= now - timedelta(hours=hours):
                return MATCH_WEIGHTS[key]
        return MATCH_WEIGHTS["published_older"]

    @staticmethod
    def _verified_points(recruitment):
        if recruitment.organization.is_verified:
            return MATCH_WEIGHTS["verified_org"]
        return 0
