from django.db.models import Q
from datetime import timedelta
from django.utils import timezone
from recruitments.models import Recruitment
from organization.services.user_organization_services import (
    UserOrganizationService
)
from core.constant import TYPE_ORGANIZATION
from connections.services.follow_services import FollowService
from connections.models import Follow
from services.geo import haversine

# Relations every recruitment card needs. Named once so the "All" tab and the
# discover sections cannot drift into different N+1 profiles.
LIST_SELECT_RELATED = ("organization", "sport")
LIST_PREFETCH_RELATED = (
    "positions__position",
    "media",
    "age_categories",
    "benefits",
)


class RecruitmentSelector:

    @staticmethod
    def list_recruitments(
        actor,
        username=None,
        sport_id=None,
        recruitment_type=None,
        status=None,
        city=None,
        search=None,
        experience_level=None,
        apply_method=None,
        birth_year=None,
        position_id=None,
        center=None,
        max_distance_km=None,
        closing_within_days=None,
        published_within_days=None,
        limit=10,
        offset=0
    ):
        """
        The "All" tab and every org-scoped listing, ordered newest-first.

        ``center``/``max_distance_km`` and ``position_id`` are the §4 discovery
        filters; they are plain queryset filters, so the org-admin and
        public-org-profile callers that never pass them get byte-for-byte the
        query they got before.
        """

        queryset = RecruitmentSelector.build_list_queryset(
            actor=actor,
            username=username,
            sport_id=sport_id,
            recruitment_type=recruitment_type,
            status=status,
            city=city,
            search=search,
            experience_level=experience_level,
            apply_method=apply_method,
            birth_year=birth_year,
            position_id=position_id,
            center=center,
            max_distance_km=max_distance_km,
            closing_within_days=closing_within_days,
            published_within_days=published_within_days,
        )

        # COUNT
        total_count = queryset.count()

        # OPTIMIZATION
        queryset = queryset.select_related(
            *LIST_SELECT_RELATED
        ).prefetch_related(
            *LIST_PREFETCH_RELATED
        )

        queryset = queryset.order_by(
            "-published_at",
            "-created_at"
        )[offset: offset + limit]

        return queryset, total_count

    @staticmethod
    def build_list_queryset(
        actor,
        username=None,
        sport_id=None,
        recruitment_type=None,
        status=None,
        city=None,
        search=None,
        experience_level=None,
        apply_method=None,
        birth_year=None,
        position_id=None,
        center=None,
        max_distance_km=None,
        closing_within_days=None,
        published_within_days=None,
    ):
        """
        The filtered candidate set — no ordering, no slicing, no prefetch.

        Split out of ``list_recruitments`` because the ranked "All" tab orders
        by a score computed in Python (§3) and therefore cannot let SQL do the
        LIMIT. Everything that decides WHICH rows are visible lives here, so
        both orderings answer over exactly the same set.
        """

        queryset = Recruitment.objects.filter(
            is_deleted=False
        )
        target_org = None

        # PROFILE FILTER
        if username:
            profile = (
                UserOrganizationService
                .get_user_or_org_by_username(
                    username
                )
            )

            # An unknown username, or a username that belongs to a PERSON,
            # scopes the list to an org that does not exist. Empty, not an
            # error — the same answer the endpoint gave before this split.
            if not profile or profile["type"] != TYPE_ORGANIZATION:
                return Recruitment.objects.none()

            target_org = profile["id"]

            queryset = queryset.filter(
                organization_id=target_org
            )

        # OWNER ACCESS
        is_owner = (
            actor
            and actor.is_org
            and target_org
            and str(actor.organization.id)
            == str(target_org)
        )

        # PUBLIC VISIBILITY RULES
        if not is_owner:
            visibility_filter = Q(
                status=Recruitment.Status.ACTIVE,
                visibility=Recruitment.Visibility.PUBLIC
            )

            # followers only support
            if actor and target_org:
                follow_filter = Q()

                # user follows org
                if actor.is_user:
                    follow_filter |= Q(
                        follower_user=actor.user,
                        following_org_id=target_org
                    )

                # org follows org
                elif actor.is_org:
                    follow_filter |= Q(
                        follower_org=actor.organization,
                        following_org_id=target_org
                    )

                follows = Follow.objects.filter(
                    follow_filter
                ).exists()

                if follows:
                    visibility_filter |= Q(
                        status=Recruitment.Status.ACTIVE,
                        visibility=(
                            Recruitment.Visibility
                            .FOLLOWERS_ONLY
                        )
                    )

            queryset = queryset.filter(
                visibility_filter
            )

        # FILTERS
        if sport_id:
            queryset = queryset.filter(
                sport_id=sport_id
            )

        if recruitment_type:
            queryset = queryset.filter(
                recruitment_type=recruitment_type
            )

        # only owner can filter drafts etc
        if status and is_owner:
            queryset = queryset.filter(
                status=status
            )

        if city:
            queryset = queryset.filter(
                city__iexact=city
            )

        # SEARCH — case-insensitive across title, short_description and the
        # organization name (OR'd). Junk is harmless — a no-match just narrows.
        if search:
            queryset = queryset.filter(
                Q(title__icontains=search)
                | Q(short_description__icontains=search)
                | Q(organization__name__icontains=search)
            )

        # EXPERIENCE LEVEL — free-text field, matched case-insensitively.
        if experience_level:
            queryset = queryset.filter(
                experience_level__icontains=experience_level
            )

        # APPLY METHOD — only honour a known value; junk is ignored (lenient,
        # same spirit as the status/apply filters elsewhere).
        if apply_method and apply_method in Recruitment.ApplyMethod.values:
            queryset = queryset.filter(
                apply_method=apply_method
            )

        # BIRTH YEAR — keep recruitments with at least one age category whose
        # range contains the year. Either bound may be null (open-ended: "born
        # 2010 or later"), and a null bound never excludes — so it is only the
        # bounds that ARE set that have to contain the year. Both conditions sit
        # in one .filter() call so they must hold for the SAME category row, not
        # one each across two of them. The related join can duplicate a
        # recruitment across matching categories, so .distinct() collapses it
        # back to one row.
        if birth_year is not None:
            queryset = queryset.filter(
                Q(age_categories__min_birth_year__isnull=True)
                | Q(age_categories__min_birth_year__lte=birth_year),
                Q(age_categories__max_birth_year__isnull=True)
                | Q(age_categories__max_birth_year__gte=birth_year),
            ).distinct()

        # POSITION — unique (recruitment, position) means the join cannot
        # duplicate a row, so no .distinct() is needed here.
        if position_id:
            queryset = queryset.filter(positions__position_id=position_id)

        # SECTION DEEP-LINKS. §5 gives every discover rail a "See all" that
        # opens the "All" tab with the rail's own rule pre-applied; these two
        # are what "Closing soon" and "New this week" mean as a filter. Without
        # them those links would land on an unfiltered list and quietly show
        # something other than what the heading promised.
        if closing_within_days:
            now = timezone.now()
            queryset = queryset.filter(
                application_deadline__gte=now,
                application_deadline__lte=now + timedelta(
                    days=closing_within_days
                ),
            )

        if published_within_days:
            queryset = queryset.filter(
                published_at__gte=timezone.now() - timedelta(
                    days=published_within_days
                )
            )

        # DISTANCE — bounding box first (it uses the existing
        # (latitude, longitude) index), then the exact haversine. Same two-step
        # as ExploreService._players_queryset. Rows with no coordinates drop out
        # of a distance-FILTERED list, which is correct: the viewer asked for
        # "within N km" and an unknown venue cannot answer that. Scoring treats
        # the same unknown as neutral (+5) precisely because it is not a filter.
        if center and max_distance_km:
            queryset = RecruitmentSelector.annotate_distance(queryset, center)
            queryset = RecruitmentSelector.filter_within_distance(
                queryset, center, max_distance_km
            )

        return queryset

    # ------------------------------------------------------------ #
    # DISTANCE (§3 / §4) — the trig itself lives in services.geo
    # ------------------------------------------------------------ #

    @staticmethod
    def annotate_distance(queryset, center):
        """
        Add ``distance_km`` from ``center`` to each row's venue coordinates.

        NOT filtered: discovery scores every candidate, and a row with no
        coordinates has to survive to collect its +5 neutral. Such a row
        annotates to NULL → ``distance_km is None`` in Python.
        """
        lat, lng = center
        return queryset.annotate(
            distance_km=haversine.distance_expr(
                lat, lng, "latitude", "longitude"
            )
        )

    @staticmethod
    def filter_within_distance(queryset, center, radius_km):
        """Box prefilter + exact circle. Expects ``annotate_distance`` first."""
        lat, lng = center
        box = haversine.bounding_box(lat, lng, radius_km)
        return queryset.filter(
            latitude__gte=box["min_lat"],
            latitude__lte=box["max_lat"],
            longitude__gte=box["min_lng"],
            longitude__lte=box["max_lng"],
            distance_km__lte=radius_km,
        )

    # ------------------------------------------------------------ #
    # DISCOVER (§4)
    # ------------------------------------------------------------ #

    @staticmethod
    def discover_candidates(context, followed_org_ids, now=None):
        """
        Every recruitment the discover sections may rank: active, live, visible
        to this viewer, and still open.

        Two deliberate differences from the "All" tab's candidate set:

          - followers-only postings from orgs this viewer follows ARE included.
            ``list_recruitments`` only widens past PUBLIC when it is scoped to
            one org's profile; here the follow set is already resolved for the
            +10 signal, so honouring the visibility rule costs nothing.
          - deadline-passed rows are excluded. They stay in "All" for badging
            (§4); a section called "Recommended for you" that opens with a trial
            that closed last week is not a recommendation.
        """
        now = now or timezone.now()

        visibility = Q(visibility=Recruitment.Visibility.PUBLIC)
        if followed_org_ids:
            visibility |= Q(
                visibility=Recruitment.Visibility.FOLLOWERS_ONLY,
                organization_id__in=followed_org_ids,
            )

        queryset = Recruitment.objects.filter(
            visibility,
            is_deleted=False,
            status=Recruitment.Status.ACTIVE,
        ).filter(
            Q(application_deadline__isnull=True)
            | Q(application_deadline__gte=now)
        )

        if context.center:
            queryset = RecruitmentSelector.annotate_distance(
                queryset, context.center
            )

        return queryset.select_related(
            *LIST_SELECT_RELATED
        ).prefetch_related(
            *LIST_PREFETCH_RELATED
        )

    @staticmethod
    def get_recruitment_for_apply(recruitment_id):
        """
        Bare fetch for the apply flow: the recruitment must exist and not be
        soft-deleted. Deliberately does NOT apply visibility/status/deadline/cap
        rules — those are re-checked authoritatively (under a row lock) inside
        ApplicationService.apply, so a closed or private recruitment still
        resolves here and the service can return a precise error instead of a
        bare 404. Questions + options are prefetched for the apply serializer's
        answer validation, age categories for its age-group validation.
        """
        return (
            Recruitment.objects
            .filter(id=recruitment_id, is_deleted=False)
            .select_related("organization")
            .prefetch_related("questions__options", "age_categories")
            .first()
        )

    @staticmethod
    def get_recruitment_detail(
        recruitment_id,
        actor
    ):

        queryset = Recruitment.objects.filter(
            id=recruitment_id,
            is_deleted=False
        )
        queryset = queryset.select_related(
            "organization",
            "sport",
            "created_by_member"
        ).prefetch_related(
            "positions__position",
            "media",
            "questions__options",
            "applications",
            "age_categories",
            "contacts",
            "benefits",
            "requirements",
            "eligibility_criteria",
        )

        recruitment = queryset.first()

        if not recruitment:
            return None

        # OWNER ACCESS
        is_owner = (
            actor
            and actor.is_org
            and str(actor.organization.id)
            == str(recruitment.organization_id)
        )

        if is_owner:
            return recruitment

        # PUBLIC ACCESS
        if (
            recruitment.status
            == Recruitment.Status.ACTIVE
            and recruitment.visibility
            == Recruitment.Visibility.PUBLIC
        ):

            return recruitment

        # FOLLOWERS ONLY
        if (
            recruitment.status
            == Recruitment.Status.ACTIVE
            and recruitment.visibility
            == Recruitment.Visibility.FOLLOWERS_ONLY
        ):

            if not actor:
                return None

            follow_filter = Q()

            if actor.is_user:

                follow_filter |= Q(
                    follower_user=actor.user,
                    following_org_id=(
                        recruitment.organization_id
                    )
                )

            elif actor.is_org:

                follow_filter |= Q(
                    follower_org=actor.organization,
                    following_org_id=(
                        recruitment.organization_id
                    )
                )

            follows = Follow.objects.filter(
                follow_filter
            ).exists()

            if follows:
                return recruitment

        return None