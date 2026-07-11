from django.db.models import Q, Exists, OuterRef

from posts.models import Post, PostHashtag


class PostSearchService:
    """
    Read-side query building for the posts search endpoint (``/posts/search``).

    A post matches the query when EITHER its ``content`` contains ``q`` OR it
    carries a hashtag whose ``name`` contains ``q`` (both case-insensitive).

    SCALE NOTE: both branches use ``icontains``, which can't use a btree index.
    The upgrade path at scale is a PostgreSQL ``pg_trgm`` GIN index on
    ``posts.content`` (and ``hashtags.name``) so the substring scans stay fast.
    That migration is intentionally NOT added here — dev runs on SQLite, which
    has no pg_trgm, and it would break the local test suite.
    """

    @staticmethod
    def search_posts_queryset(q):
        # Hashtags are stored without the leading "#", but users type "#football".
        # Strip a single leading "#" for the hashtag branch only; the content
        # branch matches the raw query as given.
        hashtag_term = q[1:] if q.startswith("#") else q

        # EXISTS subquery for the hashtag branch: a post with several matching
        # hashtags still yields ONE truthy Exists row, so the post appears once.
        # A join (post_hashtags__hashtag__name__icontains) would multiply the
        # post per matching hashtag and force a distinct() that fights the
        # keyset pagination — Exists sidesteps both.
        hashtag_match = PostHashtag.objects.filter(
            post=OuterRef("pk"),
            hashtag__name__icontains=hashtag_term,
        )

        queryset = (
            Post.objects.filter(
                is_deleted=False,
                visibility=Post.Visibility.PUBLIC,
            )
            .annotate(has_matching_hashtag=Exists(hashtag_match))
            .filter(
                Q(content__icontains=q) | Q(has_matching_hashtag=True)
            )
            # Newest first. PKs are UUIDv7 (time-sortable per CLAUDE.md), so
            # ordering by "-id" IS reverse-chronological order and is backed by
            # the PK index — do NOT "fix" this to "-created_at".
            .order_by("-id")
        )

        # Same select_related / prefetch_related shape as the trending feed
        # (ExploreTrendingPostsAPIView) so PostListSerializer needs no extra
        # queries and the item shape is identical for the frontend PostCard.
        return queryset.select_related(
            "author_user__profile",
            "author_org__profile",
            "sport",
        ).prefetch_related("media")
