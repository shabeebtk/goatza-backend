"""
Session-ranked serving for the home feed (§2 stages 4–5).

Why this is not keyset pagination
---------------------------------
The spec's serving pattern is "SQL returns ~300 candidates, Python applies the
seen-penalty / jitter / author cap, return one page". That does not survive a
cursor keyed on the SQL score: advancing the cursor past the served window
silently discards the other ~285 rows, permanently. With Goatza's current
content supply that empties the feed inside three pages.

So the ranking is computed ONCE per session and cached; pages are cut out of the
cached ordering. The cursor carries ``(seed, offset)`` — the seed both keys the
cache and drives the jitter, so a cache miss can rebuild a near-identical
ordering from the cursor alone and the offset still points somewhere sensible.
``seen_ids`` (the existing client param) is the cheap guard for the residual
drift, which is why it stays.

The ordering is a pure function of (candidates, seed, impressions). Nothing in
here may depend on request order, process identity, or wall-clock beyond the
hour bucket, or the rebuild stops being safe.
"""

import base64
import hashlib
import logging
import random
import re
import time

from django.core.cache import cache
from django.utils import timezone
from rest_framework.exceptions import NotFound

from posts.models import Post
from posts.serializers.posts_serializers import POST_MENTIONS_PREFETCH
from posts.services.saved_post_service import annotate_is_saved
from feed.selectors.feed_selectors import affinities_for, impressions_for
from feed.services.explore_services import ExploreService
from feed.services.feed_services import (
    BLEND_FOLLOWED,
    BLEND_INTEREST,
    BLEND_TRENDING,
    DECAY_OFFSET,
    GRAVITY,
    JITTER_RANGE,
    MAX_CANDIDATES,
    MAX_POSTS_PER_AUTHOR_PER_PAGE,
    PAGE_SIZE,
    RANK_CACHE_TTL_SECONDS,
    SEEN_PENALTIES,
    SESSION_BUCKET_SECONDS,
    SOURCE_FOLLOWED,
    SOURCE_INTEREST,
    SOURCE_TRENDING,
    FeedService,
)

logger = logging.getLogger(__name__)

# Fields pulled for the rank. Values-only: the full objects, with their
# select_related / prefetch chain, are fetched for the served page alone.
CANDIDATE_FIELDS = (
    "id",
    "base_score",
    "age_hours",
    "author_user_id",
    "author_org_id",
)

# A seed arrives from the client inside the cursor and is concatenated into a
# cache key, so it is validated like any other untrusted input. A rejected seed
# just means a fresh one — never an error the reader can see.
SEED_PATTERN = re.compile(r"^[A-Za-z0-9:_-]{1,80}$")

# Share of served pages written to the log with their scores. §6 tuning and any
# future Phase-3 training set both need this to have existed from day one.
RANK_LOG_SAMPLE_RATE = 0.01


def author_key(author_user_id, author_org_id):
    """
    Identity of an author for the diversity cap and for affinity.

    A club and the person who runs it are different authors — collapsing them
    would let one human take four slots on a page while looking like two.
    """
    if author_user_id:
        return str(author_user_id)
    return f"org_{author_org_id}"


class FeedRankingService:

    # ------------------------------------------------------------------ #
    # ENTRY POINT
    # ------------------------------------------------------------------ #
    @classmethod
    def get_page(cls, actor, viewer, cursor=None, seen_ids=None):
        """
        One page of the ranked feed.

        Returns ``{"posts": [...], "next_cursor": str | None}`` with
        ``feed_source`` stamped on every post.
        """
        if cursor:
            seed, offset = cls._decode_cursor(cursor)
        else:
            seed, offset = cls._new_seed(actor), 0

        cache_key = cls._cache_key(actor, seed)
        ranking = cache.get(cache_key)

        if not ranking:
            # Miss (TTL expired, or a redeploy flushed the cache). Rebuilt with
            # the seed FROM THE CURSOR, so the jitter and therefore the ordering
            # come out near-identical and the offset still lands in roughly the
            # right place.
            ranking = cls._build_ranking(actor, viewer, seed)
            cache.set(cache_key, ranking, RANK_CACHE_TTL_SECONDS)

        pages = ranking["pages"]
        page_index, page_start = cls._locate_page(pages, offset)

        if page_index is None:
            return {"posts": [], "next_cursor": None}

        page_ids = pages[page_index]
        next_offset = page_start + len(page_ids)

        # seen_ids is applied HERE and not to the candidate query: the ordering
        # has to stay a pure function of (candidates, seed, impressions) or the
        # cache-miss rebuild would return a differently-shifted list. As a serve
        # filter it does exactly the job the spec gives it — de-duplicating
        # within one scrolling session across a rebuild.
        excluded = {str(sid) for sid in (seen_ids or [])}
        served_ids = [pid for pid in page_ids if pid not in excluded]

        posts = cls._load_posts(served_ids, actor, ranking["sources"])

        total = sum(len(page) for page in pages)
        next_cursor = (
            cls._encode_cursor(seed, next_offset) if next_offset < total else None
        )

        cls._log_page(actor, seed, page_index, served_ids, ranking["scores"])

        return {"posts": posts, "next_cursor": next_cursor}

    # ------------------------------------------------------------------ #
    # RANKING
    # ------------------------------------------------------------------ #
    @classmethod
    def _build_ranking(cls, actor, viewer, seed):
        """
        Score, rerank and page the whole candidate window.

        Cached as plain JSON-able data (ids, provenance, scores) rather than
        model instances — the page's objects are re-fetched fresh every request
        so a like or an edit is never served from a ten-minute-old snapshot.
        """
        context = FeedService.resolve_actor_context(actor)
        candidates = cls._collect_candidates(actor, context)

        impressions = impressions_for(viewer, [c["id"] for c in candidates])
        affinities = affinities_for(viewer)
        now = timezone.now()

        for candidate in candidates:
            key = author_key(candidate["author_user_id"], candidate["author_org_id"])
            candidate["author_key"] = key

            # Age can come back a hair negative if a row's created_at is ahead
            # of the DB clock; clamp so the decay never inverts.
            age_hours = max(candidate["age_hours"] or 0.0, 0.0)
            decay = (age_hours + DECAY_OFFSET) ** GRAVITY

            # Affinity belongs inside relevance (§3.6), i.e. above the same
            # divisor — adding boost/decay is algebraically identical to
            # (relevance + boost) / decay and saves refetching relevance.
            score = (candidate["base_score"] or 0.0)
            boost = affinities.get(key, 0.0)
            if boost:
                score += boost / decay

            score *= cls._seen_multiplier(impressions.get(candidate["id"]), now)
            score *= cls._jitter(candidate["id"], seed)

            candidate["score"] = score

        # Two stable sorts rather than one compound key: ids are UUIDs and
        # cannot be negated inside a tuple. The tie-break is DESCENDING id,
        # matching the SQL ordering — ids are uuid7, so that means newest first.
        # It matters more than it looks: a post with no follow, no interest and
        # no engagement scores exactly 0, and every such post ties.
        ranked = sorted(candidates, key=lambda c: str(c["id"]), reverse=True)
        ranked.sort(key=lambda c: -c["score"])
        pages = cls._apply_author_cap(ranked)

        return {
            "pages": [[str(c["id"]) for c in page] for page in pages],
            "sources": {str(c["id"]): c["feed_source"] for c in ranked},
            "scores": {str(c["id"]): round(c["score"], 6) for c in ranked},
        }

    @staticmethod
    def _seen_multiplier(last_seen_at, now):
        """§3.2 — recently read posts are pushed down, never removed."""
        if not last_seen_at:
            return 1.0

        age_hours = (now - last_seen_at).total_seconds() / 3600.0
        for threshold_hours, multiplier in SEEN_PENALTIES:
            if age_hours < threshold_hours:
                return multiplier
        return 1.0

    @staticmethod
    def _jitter(post_id, seed):
        """
        §3.5 — a deterministic factor in [0.9, 1.1) from (post, seed).

        md5 and not Python's hash(): hash() is salted per process, so two
        workers would rank the same session differently and the feed would
        reshuffle on every request.
        """
        digest = hashlib.md5(f"{post_id}:{seed}".encode()).digest()
        fraction = int.from_bytes(digest[:8], "big") / float(1 << 64)
        return 1.0 - JITTER_RANGE + (2.0 * JITTER_RANGE * fraction)

    @staticmethod
    def _apply_author_cap(ranked):
        """
        §3.3 — spread posts into pages so no page holds more than two from one
        author. A REORDERING, not a filter: a post that cannot fit this page is
        pushed to the next one, so nothing is ever dropped and the result stays
        a pure function of the ranked list.

        A page may come out short when the cap cannot be satisfied — five posts
        by one author and nobody else means pages of 2, 2, 1 rather than one
        page of five. That is the cap doing its job, and it is why the cursor
        carries an offset the pages compute rather than a fixed stride.
        """
        pages = []
        counts = []

        for candidate in ranked:
            key = candidate["author_key"]
            index = 0
            while True:
                if index == len(pages):
                    pages.append([])
                    counts.append({})

                page, count = pages[index], counts[index]
                if (
                    len(page) < PAGE_SIZE
                    and count.get(key, 0) < MAX_POSTS_PER_AUTHOR_PER_PAGE
                ):
                    page.append(candidate)
                    count[key] = count.get(key, 0) + 1
                    break

                index += 1

        return pages

    # ------------------------------------------------------------------ #
    # CANDIDATES (§3.4)
    # ------------------------------------------------------------------ #
    @classmethod
    def _collect_candidates(cls, actor, context):
        """
        The blended candidate pool: ~60% followed, ~30% trending, ~10% sport
        interest, de-duplicated with followed provenance winning.

        Each source is fetched wider than its quota so the backfill has
        something to draw on. A source running dry must never shrink the feed —
        thin supply is the problem §3.4 exists to solve, not one to reproduce.
        """
        followed_quota = int(MAX_CANDIDATES * BLEND_FOLLOWED)
        trending_quota = int(MAX_CANDIDATES * BLEND_TRENDING)
        interest_quota = int(MAX_CANDIDATES * BLEND_INTEREST)

        followed = cls._as_candidates(
            FeedService.get_followed_queryset(actor, context)
        )
        # Verbatim reuse of the explore scorer — it decides WHICH strangers are
        # worth showing. The §3.1 annotations are layered on top so trending
        # rows carry a base_score comparable with the other sources; none of the
        # names collide with the ones the trending scorer already uses.
        trending = cls._as_candidates(
            FeedService.annotate_score(
                ExploreService.get_trending_posts_queryset(actor), context
            )
        )
        interest = cls._as_candidates(
            FeedService.get_interest_queryset(actor, context), limit=interest_quota
        )

        pool, taken = [], set()

        def take(rows, limit, source):
            added = 0
            for row in rows:
                if added >= limit or len(pool) >= MAX_CANDIDATES:
                    return
                if row["id"] in taken:
                    continue
                taken.add(row["id"])
                row["feed_source"] = source
                pool.append(row)
                added += 1

        take(followed, followed_quota, SOURCE_FOLLOWED)
        take(trending, trending_quota, SOURCE_TRENDING)
        take(interest, interest_quota, SOURCE_INTEREST)

        # BACKFILL — quotas are targets, not caps on the pool.
        take(trending, MAX_CANDIDATES, SOURCE_TRENDING)
        take(followed, MAX_CANDIDATES, SOURCE_FOLLOWED)

        if len(pool) < MAX_CANDIDATES:
            # Last resort, evaluated only when everything else came up short:
            # every post the actor may see, at any age. Whatever is left here is
            # by definition from someone they do not follow, so it is labelled
            # as discovery.
            take(
                cls._as_candidates(FeedService.get_feed_queryset(actor, context=context)),
                MAX_CANDIDATES,
                SOURCE_TRENDING,
            )

        return pool

    @staticmethod
    def _as_candidates(queryset, limit=MAX_CANDIDATES):
        """
        Values-only rows for the rank.

        select_related / prefetch_related are cleared first: they cost joins and
        extra queries that values() cannot use anyway, and prefetch outright
        refuses to run against dict rows.
        """
        rows = (
            queryset
            .select_related(None)
            .prefetch_related(None)
            .values(*CANDIDATE_FIELDS)[:limit]
        )
        return list(rows)

    # ------------------------------------------------------------------ #
    # SERVING
    # ------------------------------------------------------------------ #
    @staticmethod
    def _load_posts(post_ids, actor, sources):
        """
        Re-fetch the page's posts with the full serializer chain and restore the
        ranked order — ``filter(id__in=...)`` returns them in whatever order the
        DB likes, which would undo the entire rerank.
        """
        if not post_ids:
            return []

        queryset = annotate_is_saved(
            Post.objects
            .filter(id__in=post_ids, is_deleted=False)
            .select_related(
                "author_user__profile",
                "author_org__profile",
                "sport",
            )
            .prefetch_related("media", POST_MENTIONS_PREFETCH),
            actor,
        )

        by_id = {str(post.id): post for post in queryset}

        posts = []
        for post_id in post_ids:
            post = by_id.get(post_id)
            if not post:
                # Deleted between ranking and serving — the page is one shorter,
                # which is better than a hole the client has to reason about.
                continue
            post.feed_source = sources.get(post_id, SOURCE_FOLLOWED)
            posts.append(post)

        return posts

    @staticmethod
    def _locate_page(pages, offset):
        """
        ``(page_index, page_start)`` for a flat offset, or ``(None, None)`` past
        the end.

        Pages are variable length (see _apply_author_cap), so the offset is
        resolved by walking the boundaries. After a cache-miss rebuild the
        boundaries can shift by a post or two, so the first page starting AT OR
        AFTER the offset is served rather than demanding an exact hit.
        """
        start = 0
        for index, page in enumerate(pages):
            if start >= offset:
                return index, start
            start += len(page)
        return None, None

    # ------------------------------------------------------------------ #
    # CURSOR
    # ------------------------------------------------------------------ #
    @staticmethod
    def _new_seed(actor):
        """
        Stable within an hour, different between logins — which is what makes
        two logins an hour apart show a visibly different top five (§3.7).
        """
        bucket = int(time.time()) // SESSION_BUCKET_SECONDS
        return f"{FeedRankingService._actor_key(actor)}:{bucket}"

    @staticmethod
    def _actor_key(actor):
        if actor.is_user:
            return f"user_{actor.user.id}"
        return f"org_{actor.organization.id}"

    @staticmethod
    def _cache_key(actor, seed):
        # The actor prefix is derived server-side, so a forged seed can only
        # ever collide with the caller's own slot.
        return f"feed:rank:{FeedRankingService._actor_key(actor)}:{seed}"

    @staticmethod
    def _encode_cursor(seed, offset):
        return base64.b64encode(f"{seed}|{offset}".encode()).decode()

    @classmethod
    def _decode_cursor(cls, cursor):
        try:
            decoded = base64.b64decode(cursor.encode()).decode()
            seed, raw_offset = decoded.split("|")
            offset = int(raw_offset)
        except Exception:
            raise NotFound("Invalid cursor")

        if offset < 0 or offset > MAX_CANDIDATES or not SEED_PATTERN.match(seed):
            raise NotFound("Invalid cursor")

        return seed, offset

    # ------------------------------------------------------------------ #
    # OBSERVABILITY (§7)
    # ------------------------------------------------------------------ #
    @staticmethod
    def _log_page(actor, seed, page_index, post_ids, scores):
        """
        Sampled record of what was actually served, with the scores that put it
        there. Cheap, and the only way to answer "why did this rank here" once
        the knobs start moving.
        """
        if random.random() >= RANK_LOG_SAMPLE_RATE:
            return

        served = [(pid, scores.get(pid)) for pid in post_ids]
        logger.info(
            "FeedRanking | actor=%s | seed=%s | page=%s | served=%s",
            FeedRankingService._actor_key(actor), seed, page_index, served,
        )
