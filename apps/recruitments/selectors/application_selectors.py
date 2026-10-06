# recruitments/selectors/application_selectors.py
from django.db.models import Count, F, Prefetch, Q
from django.db.models.functions import ExtractYear
from apps.recruitments.models import (
    RecruitmentApplication,
    RecruitmentApplicationAnswer,
    RecruitmentApplicationStatusHistory,
)


# The birth year a filter or a sort reads, off the applicant's profile.
# BIRTH YEARS THROUGHOUT, never an age: age categories are already
# modelled in birth years, and mixing "age 14" with "born 2011" produces
# an off-by-one every January.
#
# No index on birthdate, on purpose: a recruitment has a few hundred
# applicants and this filters within ONE recruitment's rows, which the
# (recruitment, status) index already narrows. Revisit past roughly 5,000
# applications on a single recruitment.
BIRTH_YEAR = ExtractYear("applicant__profile__birthdate")

_TRUE = {"true", "1", "yes"}
_FALSE = {"false", "0", "no"}


def _as_bool(value):
    """
    A query param as a tri-state: True, False, or None for absent and for
    junk. Lenient like every other filter here — a bad value is no filter,
    never a 500 and never an unexpectedly empty list.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    return None


def _as_year(value):
    """A four-digit-ish birth year, or None for absent and for junk."""
    if value is None or value == "":
        return None
    try:
        year = int(value)
    except (TypeError, ValueError):
        return None
    return year if 1900 <= year <= 2100 else None


class ApplicationSelector:

    @staticmethod
    def list_applications(
        recruitment,
        status=None,
        search=None,
        age_category=None,
        fee_paid=None,
        birth_year_min=None,
        birth_year_max=None,
        age_mismatch=None,
        self_outcome=None,
        sort=None,
        limit=20,
        offset=0
    ):
        """
        Org-side applicants listing for a single recruitment.

        Returns (page_queryset, total_count, no_birth_year_count).
        Ownership is enforced by the caller — this only ever queries
        applications of the given recruitment.

        Two different age questions live here and must not be confused:
        ``age_category`` is the group the applicant APPLIED UNDER, and
        ``birth_year_min`` / ``birth_year_max`` is how old they actually
        ARE. An org filters by both, for different reasons.

        ``self_outcome`` is the PLAYER's own account of the trial:
        ``selected`` / ``not_selected`` / ``waiting``, plus ``attended`` /
        ``not_attended``. It narrows by what somebody SAID, never by what
        the org decided — that is ``status``.
        """
        queryset = RecruitmentApplication.objects.filter(
            recruitment=recruitment
        )

        # STATUS FILTER — one status, or a comma-separated list of them (the
        # applicants page's stage tabs). Only known values are honoured and
        # junk is dropped (lenient, same spirit as the recruitment list
        # filters) so a bad query param never 500s; a param with no known
        # value at all is no filter, exactly as a single junk value always was.
        if status:
            statuses = [
                value.strip()
                for value in status.split(",")
                if value.strip() in RecruitmentApplication.Status.values
            ]
            if statuses:
                queryset = queryset.filter(status__in=statuses)

        # AGE GROUP FILTER — the group the applicant applied under. Only honour
        # an id this recruitment actually owns: junk (or another recruitment's
        # group) is ignored the same lenient way a bad status is, and never
        # reaches the DB as a malformed UUID.
        if age_category:
            owned_category_ids = {
                str(category_id)
                for category_id in recruitment.age_categories.values_list(
                    "id", flat=True
                )
            }
            if str(age_category) in owned_category_ids:
                queryset = queryset.filter(age_category_id=age_category)

        # FEE FILTER — tri-state: absent means no filter, and so does junk.
        fee_paid = _as_bool(fee_paid)
        if fee_paid is not None:
            queryset = queryset.filter(fee_paid=fee_paid)

        # AGE MISMATCH — only ever NARROWS to the mismatched rows.
        # `age_mismatch=false` is not a filter for "the rest": the org
        # asked to see the problems, or it did not ask at all.
        if _as_bool(age_mismatch) is True:
            queryset = queryset.filter(age_mismatch_at_apply=True)

        # WHAT THE PLAYER SAID — the hint, never the decision. This is the
        # filter that makes the workflow work: the org opens the Result tab,
        # narrows to "says selected", and bulk-confirms eighteen people
        # instead of reviewing three hundred.
        #
        # Two questions in one param, because they are one question to the
        # org ("what did they tell me?"): the three outcomes, plus whether
        # they turned up at all. Junk is not a filter, the same leniency
        # every other filter here applies.
        if self_outcome in RecruitmentApplication.SelfOutcome.values:
            queryset = queryset.filter(outcome_self_reported=self_outcome)
        elif self_outcome == "attended":
            queryset = queryset.filter(attended_self_reported=True)
        elif self_outcome == "not_attended":
            queryset = queryset.filter(attended_self_reported=False)

        # SEARCH — applicant username or profile name (mirrors ListPostLikes).
        if search:
            queryset = queryset.filter(
                Q(applicant__username__icontains=search)
                | Q(applicant__profile__name__icontains=search)
            )

        # BIRTH YEAR — annotated once, then used by both the range filter
        # and the sort.
        queryset = queryset.annotate(birth_year=BIRTH_YEAR)

        birth_year_min = _as_year(birth_year_min)
        birth_year_max = _as_year(birth_year_max)

        # An applicant with no birthdate has no birth year, so a range
        # filter excludes them — silently, unless the org is told. That is
        # what no_birth_year_count is for: counted on the queryset as it
        # stands BEFORE the range narrows it, so it answers exactly "how
        # many did this range hide". Only computed when a range is
        # actually active; otherwise nothing is hidden and it stays 0.
        no_birth_year_count = 0
        if birth_year_min is not None or birth_year_max is not None:
            no_birth_year_count = queryset.filter(
                birth_year__isnull=True
            ).count()

        if birth_year_min is not None:
            queryset = queryset.filter(birth_year__gte=birth_year_min)
        if birth_year_max is not None:
            queryset = queryset.filter(birth_year__lte=birth_year_max)

        # COUNT on the filtered queryset, before slicing.
        total_count = queryset.count()

        page = queryset.select_related(
            "applicant__profile",
            "age_category",
            # The chosen date + its parent: a session inherits the
            # recruitment's venue when it sets none, so the payload needs both.
            # Its own Location rides along — the payload reads it per row.
            "session__location",
            "recruitment",
            "fee_marked_by__user__profile",
        ).order_by(
            *ApplicationSelector._ordering(sort)
        )[offset: offset + limit]

        return page, total_count, no_birth_year_count

    # Sorts the applicants list understands. Anything else — including
    # junk — falls back to newest first, the order this list has always
    # had.
    @staticmethod
    def _ordering(sort):
        """
        Applicants with no birthdate sort LAST under EITHER direction.
        Postgres would otherwise put them first on a descending sort, and
        "no birth year" is not the oldest applicant — it is an unknown,
        and an unknown belongs at the bottom of a deliberate ordering.
        """
        if sort == "birth_year":
            return (F("birth_year").asc(nulls_last=True), "-applied_at")
        if sort == "-birth_year":
            return (F("birth_year").desc(nulls_last=True), "-applied_at")
        return ("-applied_at",)

    @staticmethod
    def list_my_applications(
        user,
        status=None,
        limit=20,
        offset=0
    ):
        """
        Player-side listing of the authenticated user's own applications across
        every recruitment they applied to. Returns (page_queryset, total_count).

        Withdrawn applications are kept (the player still sees their own history
        with the withdrawn status); the status filter only narrows the list when
        a known status is passed — junk values are ignored (lenient, same spirit
        as the org-side list). The recruitment + its org/sport are select_related
        (org profile too, for the logo) so the serializer issues no extra query.
        """
        queryset = RecruitmentApplication.objects.filter(
            applicant=user
        )

        if status and status in RecruitmentApplication.Status.values:
            queryset = queryset.filter(status=status)

        # COUNT on the filtered queryset, before slicing.
        total_count = queryset.count()

        page = queryset.select_related(
            "recruitment",
            "recruitment__organization",
            "recruitment__organization__profile",
            "recruitment__sport",
            "age_category",
            "session__location",
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
        gate), answers with their question + selected option, and the status
        history with the member who made each move. Answers are ordered by
        question display_order so the serializer can regroup the per-option
        checkbox rows in the questions' natural order; history is newest first
        (id breaks a same-instant tie — UUIDv7 is time-ordered).
        """
        answers_qs = (
            RecruitmentApplicationAnswer.objects
            .select_related("question", "selected_option")
            .order_by("question__display_order", "id")
        )
        history_qs = (
            RecruitmentApplicationStatusHistory.objects
            .select_related("changed_by__user__profile")
            .order_by("-created_at", "-id")
        )

        return (
            RecruitmentApplication.objects
            .select_related(
                "applicant__profile",
                "recruitment",
                "recruitment__organization",
                "age_category",
                "session__location",
                "fee_marked_by__user__profile",
                )
            .prefetch_related(
                Prefetch("answers", queryset=answers_qs),
                Prefetch("status_history", queryset=history_qs),
            )
            .filter(id=application_id)
            .first()
        )
