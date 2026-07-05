# recruitments/selectors/application_selectors.py
from django.db.models import Q, Count, Prefetch
from recruitments.models import (
    RecruitmentApplication,
    RecruitmentApplicationAnswer,
)


class ApplicationSelector:

    @staticmethod
    def list_applications(
        recruitment,
        status=None,
        search=None,
        limit=20,
        offset=0
    ):
        """
        Org-side applicants listing for a single recruitment.

        Returns (page_queryset, total_count). Ownership is enforced by the
        caller — this only ever queries applications of the given recruitment.
        """
        queryset = RecruitmentApplication.objects.filter(
            recruitment=recruitment
        )

        # STATUS FILTER — only honour a known status; junk values are ignored
        # (lenient, same spirit as the recruitment list filters) so a bad query
        # param never 500s or empties the list unexpectedly.
        if status and status in RecruitmentApplication.Status.values:
            queryset = queryset.filter(status=status)

        # SEARCH — applicant username or profile name (mirrors ListPostLikes).
        if search:
            queryset = queryset.filter(
                Q(applicant__username__icontains=search)
                | Q(applicant__profile__name__icontains=search)
            )

        # COUNT on the filtered queryset, before slicing.
        total_count = queryset.count()

        page = queryset.select_related(
            "applicant__profile"
        ).order_by("-applied_at")[offset: offset + limit]

        return page, total_count

    @staticmethod
    def status_counts(recruitment):
        """
        {status: count} for every application status of this recruitment, in a
        single aggregate query. Statuses with no applications are included as 0
        so the frontend can render every filter chip with a count.
        """
        counts = {
            value: 0
            for value in RecruitmentApplication.Status.values
        }

        rows = (
            RecruitmentApplication.objects
            .filter(recruitment=recruitment)
            .values("status")
            .annotate(count=Count("id"))
        )

        for row in rows:
            counts[row["status"]] = row["count"]

        return counts

    @staticmethod
    def get_application_detail(application_id):
        """
        Single application with everything the detail view needs, prefetched to
        avoid N+1: applicant profile, owning recruitment/org (for the ownership
        gate), and answers with their question + selected option. Answers are
        ordered by question display_order so the serializer can regroup the
        per-option checkbox rows in the questions' natural order.
        """
        answers_qs = (
            RecruitmentApplicationAnswer.objects
            .select_related("question", "selected_option")
            .order_by("question__display_order", "id")
        )

        return (
            RecruitmentApplication.objects
            .select_related(
                "applicant__profile",
                "recruitment",
                "recruitment__organization",
            )
            .prefetch_related(
                Prefetch("answers", queryset=answers_qs)
            )
            .filter(id=application_id)
            .first()
        )
