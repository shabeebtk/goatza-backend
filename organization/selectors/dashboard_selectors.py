# organization/selectors/dashboard_selectors.py
from datetime import timedelta

from django.db.models import Count, Sum, Q, F, Case, When, IntegerField
from django.db.models.functions import TruncDate
from django.utils import timezone

from recruitments.models import Recruitment, RecruitmentApplication
from connections.models import Follow
from posts.models import Post


class DashboardSelector:
    """
    Read-only aggregations powering the organization admin dashboard.

    Everything is scoped to a single organization (the acting org, membership
    already verified by the caller) and computed with DB aggregation — no Python
    loops over rows. Soft-deleted recruitments and posts are excluded everywhere;
    follows and applications are hard-deleted / non-deletable so they need no
    such filter.
    """

    # Statuses shown in the funnel, in pipeline order. Rejected/withdrawn are
    # returned too (as muted totals) but never part of the funnel itself.
    FUNNEL_STATUSES = [
        RecruitmentApplication.Status.APPLIED,
        RecruitmentApplication.Status.REVIEWING,
        RecruitmentApplication.Status.SHORTLISTED,
        RecruitmentApplication.Status.INVITED,
        RecruitmentApplication.Status.SELECTED,
    ]

    ALLOWED_RANGES = (7, 30, 90)
    DEFAULT_RANGE = 30

    POST_SNIPPET_LEN = 140

    @classmethod
    def get_dashboard(cls, organization, range_days=DEFAULT_RANGE):
        """
        Assemble the whole dashboard payload for one organization in a handful
        of aggregate queries. `range_days` is one of ALLOWED_RANGES (clamped by
        the view); it bounds the "in range" stats, the two daily trend series
        and the top-posts window.
        """
        now = timezone.now()
        today = timezone.localdate()
        start_date = today - timedelta(days=range_days - 1)
        since = now - timedelta(days=range_days)

        # Reused base querysets (scoped + soft-delete filtered once).
        recruitments = Recruitment.objects.filter(
            organization=organization, is_deleted=False
        )
        applications = RecruitmentApplication.objects.filter(
            recruitment__organization=organization,
            recruitment__is_deleted=False,
        )

        pipeline = cls._pipeline(applications)

        return {
            "range": range_days,
            "stats": cls._stats(
                recruitments, applications, organization, since
            ),
            "pipeline": pipeline,
            "needs_attention": cls._needs_attention(
                recruitments, pipeline, now
            ),
            "recruitments_table": cls._recruitments_table(recruitments),
            "trends": {
                "applications_per_day": cls._per_day(
                    applications
                    .filter(applied_at__date__gte=start_date)
                    .annotate(day=TruncDate("applied_at"))
                    .values("day")
                    .annotate(count=Count("id")),
                    start_date,
                    today,
                ),
                "followers_per_day": cls._per_day(
                    Follow.objects
                    .filter(following_org=organization,
                            created_at__date__gte=start_date)
                    .annotate(day=TruncDate("created_at"))
                    .values("day")
                    .annotate(count=Count("id")),
                    start_date,
                    today,
                ),
            },
            "upcoming_events": cls._upcoming_events(recruitments, now),
            "top_posts": cls._top_posts(organization, since),
        }

    # ── stats ───────────────────────────────────────────────────────────────
    @staticmethod
    def _stats(recruitments, applications, organization, since):
        rec_agg = recruitments.aggregate(
            active=Count("id", filter=Q(status=Recruitment.Status.ACTIVE)),
            total_views=Sum("views_count"),
        )
        app_agg = applications.aggregate(
            total=Count("id"),
            new=Count("id", filter=Q(applied_at__gte=since)),
        )
        # Denormalized followers total; new-in-range counted live from Follow.
        profile = getattr(organization, "profile", None)
        followers_count = profile.followers_count if profile else 0
        new_followers = Follow.objects.filter(
            following_org=organization, created_at__gte=since
        ).count()

        return {
            "active_recruitments": rec_agg["active"] or 0,
            "total_applications": app_agg["total"] or 0,
            "new_applications_in_range": app_agg["new"] or 0,
            "followers_count": followers_count,
            "new_followers_in_range": new_followers,
            "total_recruitment_views": rec_agg["total_views"] or 0,
        }

    # ── pipeline ────────────────────────────────────────────────────────────
    @staticmethod
    def _pipeline(applications):
        """{status: count} for every application status, zero-filled."""
        counts = {value: 0 for value in RecruitmentApplication.Status.values}
        rows = applications.values("status").annotate(count=Count("id"))
        for row in rows:
            counts[row["status"]] = row["count"]
        return counts

    # ── needs attention ─────────────────────────────────────────────────────
    @classmethod
    def _needs_attention(cls, recruitments, pipeline, now):
        deadline_cutoff = now + timedelta(days=7)

        deadlines_soon = list(
            recruitments
            .filter(
                status=Recruitment.Status.ACTIVE,
                application_deadline__gte=now,
                application_deadline__lte=deadline_cutoff,
            )
            .order_by("application_deadline")
            .values("id", "title", "application_deadline")
        )

        drafts = list(
            recruitments
            .filter(status=Recruitment.Status.DRAFT)
            .order_by("-created_at")
            .values("id", "title")
        )

        # Active recruitments at >= 80% of their cap (only where a cap is set).
        near_capacity = list(
            recruitments
            .filter(
                status=Recruitment.Status.ACTIVE,
                max_applications__isnull=False,
                applications_count__gte=F("max_applications") * 0.8,
            )
            .order_by("-applications_count")
            .values("id", "title", "applications_count", "max_applications")
        )

        return {
            "unreviewed_applications": pipeline.get(
                RecruitmentApplication.Status.APPLIED, 0
            ),
            "deadlines_soon": deadlines_soon,
            "drafts": drafts,
            "near_capacity": near_capacity,
        }

    # ── recruitments table ──────────────────────────────────────────────────
    @classmethod
    def _recruitments_table(cls, recruitments):
        """Most recent 10 recruitments, active ones first."""
        status_order = Case(
            When(status=Recruitment.Status.ACTIVE, then=0),
            default=1,
            output_field=IntegerField(),
        )
        rows = (
            recruitments
            .annotate(_status_order=status_order)
            .order_by("_status_order", "-created_at")
            .values(
                "id", "title", "recruitment_type", "status",
                "views_count", "applications_count",
                "shortlisted_count", "selected_count",
                "application_deadline", "event_date",
            )[:10]
        )
        return [
            {
                **row,
                "conversion": cls._conversion(
                    row["applications_count"], row["views_count"]
                ),
            }
            for row in rows
        ]

    @staticmethod
    def _conversion(applications, views):
        """Applications-per-view as a percentage, one decimal place."""
        if not views:
            return 0.0
        return round((applications / views) * 100, 1)

    # ── trends ──────────────────────────────────────────────────────────────
    @staticmethod
    def _per_day(rows, start_date, today):
        """
        Zero-fill a {day: count} aggregate into a continuous daily series from
        start_date through today (inclusive). Iterates calendar days, not DB
        rows, so it stays O(range) regardless of data volume.
        """
        counts = {row["day"]: row["count"] for row in rows}
        series = []
        cursor = start_date
        while cursor <= today:
            series.append({
                "date": cursor.isoformat(),
                "count": counts.get(cursor, 0),
            })
            cursor += timedelta(days=1)
        return series

    # ── upcoming events ─────────────────────────────────────────────────────
    @staticmethod
    def _upcoming_events(recruitments, now):
        events = (
            recruitments
            .filter(
                status=Recruitment.Status.ACTIVE,
                event_date__gte=now,
            )
            .order_by("event_date")
            .prefetch_related("age_categories")[:5]
        )
        return [
            {
                "id": event.id,
                "title": event.title,
                "event_date": event.event_date,
                "venue_name": event.venue_name,
                "city": event.city,
                "age_categories": [
                    {
                        "title": category.title,
                        "reporting_time": (
                            category.reporting_time.strftime("%H:%M")
                            if category.reporting_time else None
                        ),
                    }
                    for category in event.age_categories.all()
                ],
            }
            for event in events
        ]

    # ── top posts ───────────────────────────────────────────────────────────
    @classmethod
    def _top_posts(cls, organization, since):
        posts = (
            Post.objects
            .filter(
                author_org=organization,
                is_deleted=False,
                created_at__gte=since,
            )
            .order_by("-likes_count", "-created_at")
            .prefetch_related("media")[:3]
        )
        return [
            {
                "id": post.id,
                "text": cls._snippet(post.content),
                "thumbnail": cls._first_thumbnail(post),
                "likes_count": post.likes_count,
                "comments_count": post.comments_count,
                "created_at": post.created_at,
            }
            for post in posts
        ]

    @classmethod
    def _snippet(cls, content):
        text = (content or "").strip()
        if len(text) <= cls.POST_SNIPPET_LEN:
            return text
        return text[:cls.POST_SNIPPET_LEN].rstrip() + "…"

    @staticmethod
    def _first_thumbnail(post):
        """First media's thumbnail (or the image file itself) or None."""
        media = post.media.all()
        first = media[0] if media else None
        if not first:
            return None
        return first.thumbnail_url or first.file_url or None
