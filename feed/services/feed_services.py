# feed/services/feed_services.py

from django.db.models import (
    Case, When, Value, FloatField, F, Q, ExpressionWrapper, DurationField,
)
from django.db.models.functions import Extract, Ln, Now, Power

from posts.models import Post, Like
from posts.serializers.posts_serializers import POST_MENTIONS_PREFETCH
from connections.models import Follow
from sports.models import UserSport

from organization.models import OrganizationSport


# ──────────────────────────────────────────────────────────────────────
# TUNING KNOBS (§6)
#
# Every constant below is a row in the spec's "Metrics and tuning knobs"
# table. They live in one block because tuning the feed means moving these
# numbers against each other — hunting them across four modules is how a
# ranking system stops being tunable.
# ──────────────────────────────────────────────────────────────────────

# §6 "gravity exponent" — the decay curve's steepness. Higher = faster
# content turnover, because age punishes a post harder.
GRAVITY = 1.4

# Multiplier on the engagement term. Deliberately below the old x2: with
# division-by-age doing the demotion work, engagement no longer has to be
# out-shouted by a recency bonus.
ENGAGEMENT_WEIGHT = 1.5

# §6 "comment weight" — a comment costs more effort than a like, so it is
# worth more intent.
COMMENT_WEIGHT = 2

# Hours added to a post's age before the decay divides. Without it a post
# seconds old divides by ~zero and its score dwarfs everything else on the
# page for a few minutes.
DECAY_OFFSET = 2

# §6 "seen multipliers" — (age of the impression in hours, multiplier).
# Ordered youngest-first; the first match wins. Penalise, never exclude:
# with a small content pool exclusion empties the feed, while decay lets a
# genuinely good post resurface once the sting wears off.
SEEN_PENALTIES = (
    (24, 0.2),
    (72, 0.5),
    (24 * 7, 0.8),
)

# §6 "jitter range" — ±10% of session-seeded noise. Wider = more variety,
# noisier quality.
JITTER_RANGE = 0.1

# §6 "affinity cap" — the most a favourite author can add to relevance.
# Higher makes an author effectively unmovable, which is how a feed turns
# into one person's timeline.
AFFINITY_CAP = 4.0

# §6 "blend ratio" 60 / 30 / 10 (§3.4). Shares of the candidate pool, not of
# the page — the Python rank decides what actually surfaces.
BLEND_FOLLOWED = 0.6
BLEND_TRENDING = 0.3
BLEND_INTEREST = 0.1

# Serving shape. MAX_CANDIDATES is the over-fetch window the Python rank runs
# over; PAGE_SIZE is what the client gets per request.
MAX_CANDIDATES = 300
PAGE_SIZE = 15

# §3.3 — at most this many posts by one author in any single page.
MAX_POSTS_PER_AUTHOR_PER_PAGE = 2

# How long a session's ranking survives in the cache, and how long a session
# seed stays stable. The seed bucket is the longer of the two on purpose: a
# reader who pauses for 11 minutes resumes the SAME ordering (rebuilt), not a
# reshuffled one.
RANK_CACHE_TTL_SECONDS = 600
SESSION_BUCKET_SECONDS = 3600

# Candidate provenance, surfaced to the client as `feed_source` so a post from
# a stranger can explain itself with a "Suggested" chip instead of reading as
# a bug.
SOURCE_FOLLOWED = "followed"
SOURCE_TRENDING = "trending"
SOURCE_INTEREST = "interest"


class FeedService:
    """
    Queryset construction for the home feed.

    Everything here is SQL-side: visibility, the §3.1 decay score, and the
    three candidate sources §3.4 blends. The fuzzy half of the pipeline
    (seen-penalty, jitter, author cap) lives in ``ranking_services`` where it
    can be read and tested as plain Python.
    """

    # ------------------------------------------------------------------ #
    # ACTOR CONTEXT
    # ------------------------------------------------------------------ #
    @staticmethod
    def resolve_actor_context(actor):
        """
        The follow graph + sport interests the scorer needs, as concrete lists.

        Materialized rather than left lazy: the blend runs the same id sets
        through three or four separate querysets, and a lazy values_list would
        become a re-evaluated subquery in every one of them.
        """
        if actor.is_user:
            user = actor.user

            following_user_ids = list(
                Follow.objects.filter(follower_user=user)
                .exclude(following_user__isnull=True)
                .values_list("following_user_id", flat=True)
            )
            following_org_ids = list(
                Follow.objects.filter(follower_user=user)
                .exclude(following_org__isnull=True)
                .values_list("following_org_id", flat=True)
            )
            sport_ids = list(
                UserSport.objects.filter(user=user).values_list("sport_id", flat=True)
            )
            primary_sport_ids = list(
                UserSport.objects.filter(user=user, is_primary=True)
                .values_list("sport_id", flat=True)
            )

        else:  # actor.is_org
            org = actor.organization

            following_user_ids = list(
                Follow.objects.filter(follower_org=org)
                .exclude(following_user__isnull=True)
                .values_list("following_user_id", flat=True)
            )
            following_org_ids = list(
                Follow.objects.filter(follower_org=org)
                .exclude(following_org__isnull=True)
                .values_list("following_org_id", flat=True)
            )
            # An org's associated sports stand in for a person's interests.
            sport_ids = list(
                OrganizationSport.objects.filter(organization=org)
                .values_list("sport_id", flat=True)
            )
            primary_sport_ids = list(
                OrganizationSport.objects.filter(organization=org, is_primary=True)
                .values_list("sport_id", flat=True)
            )

        return {
            "following_user_ids": following_user_ids,
            "following_org_ids": following_org_ids,
            "sport_ids": sport_ids,
            "primary_sport_ids": primary_sport_ids,
        }

    # ------------------------------------------------------------------ #
    # SCORING (§3.1)
    # ------------------------------------------------------------------ #
    @staticmethod
    def annotate_score(queryset, context):
        """
        Add the §3.1 gravity score to any post queryset.

            relevance  = follow + primary_interest + secondary_interest
                       + 1.5 * ln(1 + likes + 2*comments)
            base_score = relevance / (age_hours + 2) ** 1.4

        The old model added a recency bonus that expired after 24h while
        ln(engagement) kept growing, so a post with ~20 interactions outranked
        every fresh post forever. Dividing by age instead means no amount of
        engagement outlives the decay — which is the whole fix.

        Named ``base_score`` and not ``final_score``: ``final_score`` is the
        post-rerank value in Python, and keeping the two names apart is what
        stops them being confused. Every arithmetic annotation carries an
        explicit output_field — Django will not infer one across mixed types
        and fails at query time, not import time.

        Safe to apply on top of the explore/trending scorer: none of these
        names collide with its annotations.
        """
        age = ExpressionWrapper(
            Now() - F("created_at"),
            output_field=DurationField(),
        )

        queryset = queryset.annotate(
            follow_score=Case(
                When(author_user_id__in=context["following_user_ids"], then=Value(6)),
                When(author_org_id__in=context["following_org_ids"], then=Value(6)),
                default=Value(0),
                output_field=FloatField(),
            ),
            primary_interest_score=Case(
                When(sport_id__in=context["primary_sport_ids"], then=Value(5)),
                default=Value(0),
                output_field=FloatField(),
            ),
            secondary_interest_score=Case(
                When(sport_id__in=context["sport_ids"], then=Value(3)),
                default=Value(0),
                output_field=FloatField(),
            ),
            engagement_boost=ExpressionWrapper(
                Value(ENGAGEMENT_WEIGHT) * Ln(
                    Value(1.0)
                    + F("likes_count")
                    + Value(COMMENT_WEIGHT) * F("comments_count"),
                    output_field=FloatField(),
                ),
                output_field=FloatField(),
            ),
            age_hours=ExpressionWrapper(
                Extract(age, "epoch") / Value(3600.0),
                output_field=FloatField(),
            ),
        )

        # Second pass: an annotation cannot reference a sibling declared in the
        # same annotate() call.
        queryset = queryset.annotate(
            relevance=ExpressionWrapper(
                F("follow_score")
                + F("primary_interest_score")
                + F("secondary_interest_score")
                + F("engagement_boost"),
                output_field=FloatField(),
            ),
        )

        return queryset.annotate(
            base_score=ExpressionWrapper(
                F("relevance") / Power(
                    F("age_hours") + Value(float(DECAY_OFFSET)),
                    Value(GRAVITY),
                ),
                output_field=FloatField(),
            ),
        )

    # ------------------------------------------------------------------ #
    # VISIBILITY
    # ------------------------------------------------------------------ #
    @staticmethod
    def visible_posts_queryset(actor, context, seen_ids=None):
        """
        Every live post this actor is allowed to see. Unscored — the callers
        decide whether they want the §3.1 annotations.
        """
        queryset = Post.objects.filter(is_deleted=False)

        # Public posts are always visible
        visibility_filter = Q(visibility=Post.Visibility.PUBLIC)

        # Followers-only posts from followed users/orgs
        visibility_filter |= Q(
            visibility=Post.Visibility.FOLLOWERS,
            author_user_id__in=context["following_user_ids"],
        )
        visibility_filter |= Q(
            visibility=Post.Visibility.FOLLOWERS,
            author_org_id__in=context["following_org_ids"],
        )

        # Own posts are always visible
        if actor.is_user:
            visibility_filter |= Q(author_user=actor.user)
        else:
            visibility_filter |= Q(author_org=actor.organization)

        queryset = queryset.filter(visibility_filter)

        if seen_ids:
            queryset = queryset.exclude(id__in=seen_ids)

        return queryset

    # ------------------------------------------------------------------ #
    # CANDIDATE SOURCES (§3.4)
    # ------------------------------------------------------------------ #
    @classmethod
    def get_feed_queryset(cls, actor, seen_ids=None, context=None):
        """
        Everything visible to the actor, scored and ordered. This is the
        widest source — the blend uses it as the last-resort backfill so the
        feed cannot render empty just because the followed graph and the
        30-day trending window are both dry.
        """
        context = context or cls.resolve_actor_context(actor)

        queryset = cls.annotate_score(
            cls.visible_posts_queryset(actor, context, seen_ids), context
        ).order_by("-base_score", "-id")

        return queryset.select_related(
            "author_user__profile",
            "author_org__profile",   # org feed needs org profile too
            "sport",
        ).prefetch_related("media", POST_MENTIONS_PREFETCH)

    @classmethod
    def get_followed_queryset(cls, actor, context, seen_ids=None):
        """
        Source 1 (~60%): posts by accounts the actor actually follows, plus
        their own.

        Narrower than ``get_feed_queryset``, which also carries every public
        post network-wide. That width is what made the blend a no-op in the
        first draft — and it is also why ``feed_source`` would have labelled a
        total stranger's post "followed". Strangers come in through the
        trending and interest sources, which is exactly what §3.4 describes.
        """
        queryset = cls.visible_posts_queryset(actor, context, seen_ids)

        followed_filter = (
            Q(author_user_id__in=context["following_user_ids"])
            | Q(author_org_id__in=context["following_org_ids"])
        )
        if actor.is_user:
            followed_filter |= Q(author_user=actor.user)
        else:
            followed_filter |= Q(author_org=actor.organization)

        return cls.annotate_score(
            queryset.filter(followed_filter), context
        ).order_by("-base_score", "-id")

    @classmethod
    def get_interest_queryset(cls, actor, context, seen_ids=None):
        """
        Source 3 (~10%): public posts tagged with one of the actor's sports,
        from authors they do NOT follow. Pure discovery inside the actor's own
        game — the narrowest of the three sources and the one most likely to
        be empty, which is why it is taken last.
        """
        # No early `.none()` for a sportless actor: an empty sport_ids list
        # already yields no rows, and none() would drop the §3.1 annotations the
        # caller reads off every candidate.
        queryset = Post.objects.filter(
            is_deleted=False,
            visibility=Post.Visibility.PUBLIC,
            sport_id__in=context["sport_ids"],
        ).exclude(
            Q(author_user_id__in=context["following_user_ids"])
            | Q(author_org_id__in=context["following_org_ids"])
        )

        # Never recommend the actor their own post as a discovery.
        if actor.is_user:
            queryset = queryset.exclude(author_user=actor.user)
        else:
            queryset = queryset.exclude(author_org=actor.organization)

        if seen_ids:
            queryset = queryset.exclude(id__in=seen_ids)

        return cls.annotate_score(queryset, context).order_by("-base_score", "-id")

    # ------------------------------------------------------------------ #
    # REACTIONS HELPER
    # ------------------------------------------------------------------ #
    @staticmethod
    def get_actor_reactions(actor, post_ids):
        """
        Fetch reactions for the current actor (user or org).
        """
        if actor.is_user:
            reactions = Like.objects.filter(
                user=actor.user,
                post_id__in=post_ids
            ).values("post_id", "type")
        else:
            reactions = Like.objects.filter(
                organization=actor.organization,
                post_id__in=post_ids
            ).values("post_id", "type")

        return {r["post_id"]: r["type"] for r in reactions}
