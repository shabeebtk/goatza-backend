# recruitments/selectors/application_selectors.py
import uuid
from datetime import date, datetime, time, timedelta

from django.db.models import Count, Exists, F, OuterRef, Prefetch, Q
from django.db.models.functions import ExtractYear
from apps.accounts.models import UserProfile
from apps.highlights.models import Highlight
from apps.highlights.selectors.highlight_selectors import recruiter_visible_q
from apps.recruitments.models import (
    RecruitmentApplication,
    RecruitmentApplicationAnswer,
    RecruitmentApplicationStatusHistory,
)
from apps.sports.models import SportPosition, UserSportPosition


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

# The profile's own column width — a longer "city" cannot match anything.
CITY_FILTER_MAX = 100

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


def _as_uuid(value):
    """A UUID, or None — junk must never reach the DB as a malformed id."""
    if not value:
        return None
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _as_date(value):
    """A "YYYY-MM-DD" calendar day, or None for absent and for junk."""
    if not value:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError:
        return None


class ApplicationSelector:

    @staticmethod
    def filtered_applications(
        recruitment,
        status=None,
        search=None,
        age_category=None,
        session_id=None,
        fee_paid=None,
        birth_year_min=None,
        birth_year_max=None,
        age_mismatch=None,
        self_outcome=None,
        gender=None,
        position=None,
        city=None,
        has_highlights=None,
        applied_from=None,
        applied_to=None,
    ):
        """
        Every filter the org-side applicants list understands, and nothing
        else: no count, no order, no slice. Returns
        ``(queryset, no_birth_year_count)``.

        THE ONE PIPELINE. The list and the Excel export both call this, so
        "download this view" is exactly the rows the org is looking at — two
        copies of these filters would agree until somebody edited one.

        Ownership is enforced by the caller — this only ever queries
        applications of the given recruitment. Every filter is LENIENT: junk
        is no filter, never a 500 and never an unexpectedly empty list.

        Two different age questions live here and must not be confused:
        ``age_category`` is the group the applicant APPLIED UNDER, and
        ``birth_year_min`` / ``birth_year_max`` is how old they actually
        ARE. An org filters by both, for different reasons.

        ``self_outcome`` is the PLAYER's own account of the trial:
        ``selected`` / ``not_selected`` / ``waiting``, plus ``attended`` /
        ``not_attended``. It narrows by what somebody SAID, never by what
        the org decided — that is ``status``.

        ``session_id`` is WHICH CENTRE they said they were coming to, on a
        trial that visits several. It is the gate list: an org running a
        four-city tour checks in the Kozhikode players at Kozhikode, and
        three quarters of this list is nothing to do with them.
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

        # CENTRE FILTER — the date/centre the applicant picked. Same rule as
        # the age group above: only honour an id this recruitment actually
        # owns, so junk (or another recruitment's session) is ignored rather
        # than 500ing on a malformed UUID or, worse, leaking that some other
        # org's session id exists by filtering on it.
        if session_id:
            owned_session_ids = {
                str(value)
                for value in recruitment.sessions.values_list("id", flat=True)
            }
            if str(session_id) in owned_session_ids:
                queryset = queryset.filter(session_id=session_id)

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

        # GENDER — off the PROFILE, the only place it is recorded.
        if gender in UserProfile.Gender.values:
            queryset = queryset.filter(applicant__profile__gender=gender)

        # POSITION — a position the player LISTS for this recruitment's sport,
        # not the one they applied as. Only an id belonging to that sport is
        # honoured, so another sport's id is ignored rather than emptying the
        # list. Exists, not a join: a join would repeat rows.
        position_id = _as_uuid(position)
        if position_id and SportPosition.objects.filter(
            id=position_id, sport_id=recruitment.sport_id
        ).exists():
            queryset = queryset.filter(
                Exists(
                    UserSportPosition.objects.filter(
                        user_id=OuterRef("applicant_id"),
                        sport_id=recruitment.sport_id,
                        position_id=position_id,
                    )
                )
            )

        # CITY — the profile's own city, as a substring: "kochi" finds
        # "Kochi, Kerala".
        city = (city or "").strip()[:CITY_FILTER_MAX]
        if city:
            queryset = queryset.filter(applicant__profile__city__icontains=city)

        # HAS HIGHLIGHTS — something a RECRUITER can watch, by the same rule
        # the row's "▶ Highlights (n)" chip counts with (recruiter_visible_q).
        # Narrows only, like age_mismatch.
        if _as_bool(has_highlights) is True:
            queryset = queryset.filter(
                Exists(
                    Highlight.objects.filter(
                        recruiter_visible_q(), user_id=OuterRef("applicant_id")
                    )
                )
            )

        # APPLIED BETWEEN — whole calendar days IN THE RECRUITMENT'S ZONE,
        # both ends inclusive: "the 12th" means the 12th at the ground, not on
        # the server's UTC clock. A backwards range is junk, and ignored.
        applied_from = _as_date(applied_from)
        applied_to = _as_date(applied_to)
        if applied_from and applied_to and applied_from > applied_to:
            applied_from = applied_to = None
        tz = recruitment.zoneinfo
        if applied_from:
            queryset = queryset.filter(
                applied_at__gte=datetime.combine(applied_from, time.min, tzinfo=tz)
            )
        if applied_to:
            queryset = queryset.filter(
                applied_at__lt=datetime.combine(
                    applied_to + timedelta(days=1), time.min, tzinfo=tz
                )
            )

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

        return queryset, no_birth_year_count

    @staticmethod
    def list_applications(
        recruitment,
        sort=None,
        limit=20,
        offset=0,
        **filters,
    ):
        """
        Org-side applicants listing for a single recruitment.

        Returns (page_queryset, total_count, no_birth_year_count). The
        filtering is ``filtered_applications``; this only counts, orders and
        slices, so the list and the export can never filter differently.
        """
        queryset, no_birth_year_count = (
            ApplicationSelector.filtered_applications(recruitment, **filters)
        )

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

    @staticmethod
    def export_queryset(recruitment, scope="filtered", filters=None):
        """
        Every row a download may draw from, in DOWNLOAD order. The caller
        counts it and slices one batch of 100.

        ``scope="filtered"`` is exactly the list the org is looking at — its
        tab, search and every filter, through ``filtered_applications``.
        ``scope="all"`` is everybody who has not withdrawn, whatever the
        screen says.

        ALWAYS ``applied_at, id`` ASCENDING, whatever the list is sorted by:
        a new applicant then lands at the END, so rows 1–100 are still the
        same 100 people when the org comes back for 101–200. Newest-first
        would shift every batch by one per new application.
        """
        if scope == "all":
            queryset = (
                RecruitmentApplication.objects
                .filter(recruitment=recruitment)
                .exclude(status=RecruitmentApplication.Status.WITHDRAWN)
                .annotate(birth_year=BIRTH_YEAR)
            )
        else:
            queryset, _ = ApplicationSelector.filtered_applications(
                recruitment, **(filters or {})
            )

        # One query for every answer on the batch, with the question and the
        # chosen option on each — never one per cell.
        answers_qs = (
            RecruitmentApplicationAnswer.objects
            .select_related("question", "selected_option")
            .order_by(
                "question__display_order",
                "selected_option__display_order",
                "id",
            )
        )

        return (
            queryset
            .select_related(
                "applicant__profile",
                "age_category",
                "session__location",
                "recruitment",
                "fee_marked_by__user__profile",
            )
            .prefetch_related(Prefetch("answers", queryset=answers_qs))
            .order_by("applied_at", "id")
        )

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
        The recruitment's media is prefetched for the row's cover — one query
        per page, and the default ordering is the same "first media" the list
        card picks.
        """
        # NO is_deleted FILTER, DELIBERATELY. A recruitment the club deleted
        # stays on the player's Applied tab — it is their history, and the
        # card shows it greyed with the club's note (is_removed /
        # removed_note) instead of a link that would 404.
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
        ).prefetch_related(
            "recruitment__media",
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
