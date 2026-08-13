# recruitments/services/discover_service.py
"""
Recruitment discovery (spec §4): the four sections behind
``GET /recruitments/discover``, and the ranked ordering behind the "All" tab.

Shape of a request:

    resolve the viewer once  →  one candidate query (distance annotated in SQL)
    →  score every row in Python  →  cut into sections  →  dedup  →  cache

Scoring in Python over the whole candidate set is the §7 phase-1 design, and it
holds because the ACTIVE recruitment corpus stays in the hundreds to low
thousands however many users there are (§1). Cost grows with recruitments, not
with pageviews.
"""

import logging
from dataclasses import dataclass
from datetime import timedelta

from django.core.cache import cache
from django.db.models import F, Q
from django.utils import timezone

from recruitments.models import RecruitmentDiscoverImpression
from recruitments.selectors.player_context_selectors import (
    PlayerContextSelector,
)
from recruitments.selectors.recruitment_selectors import (
    LIST_PREFETCH_RELATED,
    LIST_SELECT_RELATED,
    RecruitmentSelector,
)
from recruitments.services import eligibility_service
from recruitments.services.match_score_service import MatchScoreService

logger = logging.getLogger(__name__)

# §4 section keys, in dedup priority order. A recruitment that qualifies for
# "recommended" must not turn up again three rails down.
SECTION_RECOMMENDED = "recommended"
SECTION_CLOSING_SOON = "closing_soon"
SECTION_NEAR_YOU = "near_you"
SECTION_NEW_THIS_WEEK = "new_this_week"

SECTION_ORDER = (
    SECTION_RECOMMENDED,
    SECTION_CLOSING_SOON,
    SECTION_NEAR_YOU,
    SECTION_NEW_THIS_WEEK,
)

SECTION_LIMIT = 10

CLOSING_SOON_DAYS = 7
NEW_THIS_WEEK_DAYS = 7
DEFAULT_MAX_DISTANCE_KM = 50
MAX_DISTANCE_KM_CEILING = 500

# §4: "cache the discover payload per user for 10 minutes; invalidate lazily —
# freshness signals tolerate it". django.core.cache is Redis in production and
# LocMem in dev, so nothing here imports redis.
CACHE_TTL_SECONDS = 600
CACHE_VERSION = "v1"

# The corpus is supposed to stay in the low thousands (§1). If it ever doesn't,
# scoring everything per request stops being free — so bound the work and SAY
# SO in the logs rather than silently ranking a truncated set. Crossing this is
# the trigger for the §7 phase-2 materialization, not something to paper over.
MAX_SCORED_CANDIDATES = 3000


@dataclass(frozen=True)
class DiscoverPayload:
    """Sections plus the context the client needs to explain them."""

    sections: dict
    context: object
    max_distance_km: int

    @property
    def is_personalized(self):
        return self.context.is_personalized

    @property
    def missing_fields(self):
        return self.context.missing_fields


class RecruitmentDiscoverService:

    # ------------------------------------------------------------ #
    # DISCOVER (§4)
    # ------------------------------------------------------------ #

    @classmethod
    def discover(cls, actor, max_distance_km=None, now=None):
        """
        Build the four sections for ``actor``.

        Works for an org actor and for a player with an empty profile: both get
        a PlayerContext whose personalized fields are empty, every candidate
        then scores identically on sport/position, and the ordering falls
        through to the non-personalized signals (freshness, deadline, distance).
        A valid payload plus ``is_personalized: false`` is a far better answer
        than a 400 the client would have to special-case.
        """
        now = now or timezone.now()
        max_distance_km = cls.normalize_max_distance(max_distance_km)

        context = PlayerContextSelector.resolve(actor)

        candidates = list(
            RecruitmentSelector.discover_candidates(
                context, context.followed_org_ids, now=now
            )[:MAX_SCORED_CANDIDATES]
        )
        if len(candidates) == MAX_SCORED_CANDIDATES:
            logger.warning(
                "RecruitmentDiscoverService | candidate cap hit | "
                f"scored={MAX_SCORED_CANDIDATES} — ranking is over a truncated "
                "set; time for the §7 phase-2 materialization"
            )

        scored = MatchScoreService.score_all(candidates, context, now)

        sections = cls._build_sections(scored, max_distance_km, now)

        return DiscoverPayload(
            sections=sections,
            context=context,
            max_distance_km=max_distance_km,
        )

    @classmethod
    def _build_sections(cls, scored, max_distance_km, now):
        """
        Cut the scored candidates into §4's four sections and dedup across them
        in priority order.

        Dedup happens BEFORE the cap, not after: taking each section's top 10
        and then removing overlaps would leave half-empty rails whenever the
        strongest matches also happen to be the nearest ones — which is the
        normal case, not the edge case.
        """
        eligible = [pair for pair in scored if pair[1].is_eligible]

        # "Recommended" is the only section ineligible rows can reach. They sink
        # to the bottom of it (×0.05) and keep their badge — §2's "rank down,
        # never hide". The other three answer a factual question ("closing
        # soon", "near you"), and an answer the player cannot act on is noise.
        ordered = {
            SECTION_RECOMMENDED: sorted(
                scored, key=lambda pair: -pair[1].score
            ),
            SECTION_CLOSING_SOON: sorted(
                (
                    pair for pair in eligible
                    if cls._closes_within(pair[0], CLOSING_SOON_DAYS, now)
                ),
                key=lambda pair: pair[0].application_deadline,
            ),
            SECTION_NEAR_YOU: sorted(
                (
                    pair for pair in eligible
                    if pair[1].distance_km is not None
                    and pair[1].distance_km <= max_distance_km
                ),
                key=lambda pair: pair[1].distance_km,
            ),
            SECTION_NEW_THIS_WEEK: sorted(
                (
                    pair for pair in eligible
                    if cls._published_within(pair[0], NEW_THIS_WEEK_DAYS, now)
                ),
                key=lambda pair: pair[0].published_at,
                reverse=True,
            ),
        }

        taken = set()
        sections = {}
        for key in SECTION_ORDER:
            picked = []
            for recruitment, match in ordered[key]:
                if recruitment.id in taken:
                    continue
                picked.append((recruitment, match))
                taken.add(recruitment.id)
                if len(picked) == SECTION_LIMIT:
                    break
            sections[key] = picked

        return sections

    @staticmethod
    def _closes_within(recruitment, days, now):
        deadline = recruitment.application_deadline
        return bool(deadline and now <= deadline <= now + timedelta(days=days))

    @staticmethod
    def _published_within(recruitment, days, now):
        published_at = recruitment.published_at
        return bool(published_at and published_at >= now - timedelta(days=days))

    # ------------------------------------------------------------ #
    # CACHE (§4)
    # ------------------------------------------------------------ #

    @staticmethod
    def cache_key(actor, max_distance_km):
        """
        Per-actor, per-filter. Keyed on the ACTOR and not the user, because the
        same person browsing as their club gets a different payload (different
        location, different follow graph) and must not be served the player one.
        """
        if actor is None:
            who = "anon"
        elif actor.is_user:
            who = f"u:{actor.user.id}"
        else:
            who = f"o:{actor.organization.id}"
        return f"recruit:discover:{CACHE_VERSION}:{who}:d{max_distance_km}"

    @staticmethod
    def get_cached(key):
        return cache.get(key)

    @staticmethod
    def set_cached(key, payload):
        cache.set(key, payload, CACHE_TTL_SECONDS)

    @staticmethod
    def normalize_max_distance(value):
        """Lenient int parse — junk falls back to the default, never a 400."""
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return DEFAULT_MAX_DISTANCE_KM
        if parsed <= 0:
            return DEFAULT_MAX_DISTANCE_KM
        return min(parsed, MAX_DISTANCE_KM_CEILING)

    # ------------------------------------------------------------ #
    # METRICS (§8)
    # ------------------------------------------------------------ #

    @classmethod
    def record_impressions(cls, actor, sections, now=None):
        """
        Log (player, recruitment, score, section) for a served page.

        Written on cache MISS only. The cached payload is literally the same
        page, so the 10-minute cache window doubles as the de-duplication
        window for "this was served" — and paying 40 inserts on a response that
        otherwise costs one Redis read would be the most expensive thing on the
        endpoint.

        Fire-and-forget: a metrics failure must never turn a working discover
        page into a 500.
        """
        if actor is None or not actor.is_user:
            # Org actors browse discovery; they do not generate player-outcome
            # training data, and §8's metrics are all per-player.
            return 0

        now = now or timezone.now()

        rows = [
            (section, recruitment, match)
            for section in SECTION_ORDER
            for recruitment, match in sections.get(section, [])
        ]
        if not rows:
            return 0

        try:
            # UPDATE-then-INSERT, the same shape as FeedImpressionService.record:
            # ON CONFLICT DO UPDATE assigns the EXCLUDED value, so it would reset
            # served_count to 1 instead of counting it.
            seen_filter = Q()
            for section, recruitment, _ in rows:
                seen_filter |= Q(section=section, recruitment_id=recruitment.id)

            RecruitmentDiscoverImpression.objects.filter(
                seen_filter, user=actor.user
            ).update(served_count=F("served_count") + 1, last_served_at=now)

            RecruitmentDiscoverImpression.objects.bulk_create(
                [
                    RecruitmentDiscoverImpression(
                        user=actor.user,
                        recruitment=recruitment,
                        section=section,
                        match_score=match.score,
                        is_eligible=match.is_eligible,
                        first_served_at=now,
                        last_served_at=now,
                    )
                    for section, recruitment, match in rows
                ],
                ignore_conflicts=True,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                f"RecruitmentDiscoverService | impression log failed | {exc}"
            )
            return 0

        return len(rows)

    # ------------------------------------------------------------ #
    # "ALL" TAB (§4) — same scorer, flat list
    # ------------------------------------------------------------ #

    @classmethod
    def ranked_list(
        cls,
        actor,
        filters,
        age_eligible=False,
        limit=10,
        offset=0,
        now=None,
    ):
        """
        The "All" tab for an authenticated player: the same filters as
        ``list_recruitments``, ordered by ``-match_score``.

        Ordering by a Python-computed score means SQL cannot do the LIMIT, so
        the filtered set is materialized and sliced here. That is the same
        bounded cost as discover, over the same corpus.

        Deadline-passed rows stay in this list — they carry the "Applications
        closed" badge, which is the whole reason "All" keeps them.

        Returns ([(recruitment, MatchResult), ...], total_count).
        """
        now = now or timezone.now()
        context = PlayerContextSelector.resolve(actor)

        queryset = RecruitmentSelector.build_list_queryset(
            actor=actor,
            center=context.center,
            **filters,
        )

        if context.center and not filters.get("max_distance_km"):
            # build_list_queryset only annotates distance when it is also
            # filtering by it; the score wants it either way.
            queryset = RecruitmentSelector.annotate_distance(
                queryset, context.center
            )

        candidates = list(
            queryset.select_related(
                *LIST_SELECT_RELATED
            ).prefetch_related(
                *LIST_PREFETCH_RELATED
            )[:MAX_SCORED_CANDIDATES]
        )

        scored = MatchScoreService.score_all(candidates, context, now)

        if age_eligible:
            # The ONE place a verdict filters instead of ranking, because here
            # the player ticked "show me what I'm eligible for". Age is the
            # toggle's name and its only subject: a closed or gender-restricted
            # posting is still theirs to see. A player with no birthdate keeps
            # everything — unknown is not ineligible (see eligibility_service).
            scored = [
                pair for pair in scored
                if eligibility_service.REASON_AGE not in pair[1].verdict.reasons
            ]

        # Stable within equal scores: newest first, matching the unranked list's
        # tiebreak so two adjacent pages never reshuffle.
        scored.sort(
            key=lambda pair: (
                -pair[1].score,
                -(pair[0].published_at or pair[0].created_at).timestamp(),
            )
        )

        total_count = len(scored)
        return scored[offset: offset + limit], total_count
