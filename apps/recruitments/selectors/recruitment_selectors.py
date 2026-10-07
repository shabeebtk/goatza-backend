from django.db.models import (
    Case, DateTimeField, F, FloatField, IntegerField, OuterRef, Q, Subquery,
    UUIDField, Value, When,
)
from django.db.models.functions import Coalesce
from datetime import timedelta
from django.utils import timezone
from apps.recruitments.models import Recruitment, TrialSession
from apps.organization.services.user_organization_services import (
    UserOrganizationService
)
from core.constant import TYPE_ORGANIZATION
from apps.connections.services.follow_services import FollowService
from apps.connections.models import Follow
from apps.recruitments.selectors.saved_recruitment_selectors import (
    SavedRecruitmentSelector
)
from apps.recruitments.trial_window import live_session_q, trial_not_over_q
from services.geo import haversine

# Relations every recruitment card needs. Named once so the "All" tab and the
# discover sections cannot drift into different N+1 profiles.
LIST_SELECT_RELATED = ("organization", "sport")
LIST_PREFETCH_RELATED = (
    "positions__position",
    "media",
    "age_categories",
    "benefits",
    # The location comes along with the dates: trial_session_payload reads
    # it for the editor's own_location key, so without it that key costs a
    # query per date on every card.
    "sessions__location",
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
        offset=0,
        now=None,
    ):
        """
        The "All" tab and every org-scoped listing, in TRIAL order — see
        ``order_for_list``. Not "newest posted": that read as unordered, because
        when a trial was typed up says nothing about when it happens.

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
            now=now,
        )

        # COUNT
        total_count = queryset.count()

        # OPTIMIZATION
        queryset = queryset.select_related(
            *LIST_SELECT_RELATED
        ).prefetch_related(
            *LIST_PREFETCH_RELATED
        )

        queryset = RecruitmentSelector.order_for_list(
            queryset, now=now
        )[offset: offset + limit]

        return queryset, total_count

    # ------------------------------------------------------------ #
    # ORDERING — the plain list only
    # ------------------------------------------------------------ #

    # Buckets, in the order a reader wants them. Named so the ordering can be
    # asserted and explained without decoding integers.
    BUCKET_DRAFT = 0
    BUCKET_ACCEPTING = 1
    BUCKET_UPCOMING_CLOSED = 2
    BUCKET_FINISHED = 3

    @staticmethod
    def order_for_list(queryset, now=None):
        """
        Order a listing by WHAT IT IS and WHEN IT HAPPENS, not by when it was
        posted.

        ``-published_at`` ranked a June posting for October above last week's
        posting for this Saturday, and let a finished trial sit above a live
        one. For the reader that is not an order at all.

        Four buckets, then a date inside each:

          0  drafts (only the owner is ever shown one)
          1  accepting  — active, trial window open, deadline not passed
          2  upcoming   — active, trial still ahead, no longer accepting
          3  finished   — trial over, closed, or cancelled

        Buckets 1 and 2 read SOONEST first: the next trial to happen belongs at
        the top, and a "Looking for players" posting with only a deadline falls
        in by that deadline. Buckets 0 and 3 read MOST RECENT first — a draft by
        when it was last touched, a finished trial by when it finished.

        THE TRAP, and the reason for two sort columns: two buckets sort ASC and
        two DESC, so one key cannot serve both. Each column is NULL outside the
        buckets it serves and both carry ``nulls_last``, so a row is only ever
        positioned by its own bucket's key and the other column is a constant
        tie it never reaches. Collapsing these into one would hand the finished
        trials back oldest-first — this bug, inverted.

        Done in SQL, never in Python: the list is offset-paginated, and a Python
        sort would only ever reorder the page it was handed.
        """
        active_q = Q(status=Recruitment.Status.ACTIVE)
        # Reused, not restated — the ONE definition of "the trial window has
        # not closed" (trial_window.py), the same one the non-owner branch
        # filters on above.
        upcoming_q = active_q & trial_not_over_q(now)
        deadline_open_q = (
            Q(application_deadline__isnull=True)
            | Q(application_deadline__gte=(now or timezone.now()))
        )

        bucket = Case(
            When(
                status=Recruitment.Status.DRAFT,
                then=Value(RecruitmentSelector.BUCKET_DRAFT),
            ),
            When(
                upcoming_q & deadline_open_q,
                then=Value(RecruitmentSelector.BUCKET_ACCEPTING),
            ),
            When(
                upcoming_q,
                then=Value(RecruitmentSelector.BUCKET_UPCOMING_CLOSED),
            ),
            # Everything left: closed, cancelled, or an active row whose trial
            # day has passed.
            default=Value(RecruitmentSelector.BUCKET_FINISHED),
            output_field=IntegerField(),
        )

        # Buckets 1 + 2 only. `event_date` is the first session; a posting with
        # no trial day at all falls in by its deadline instead.
        sort_upcoming = Case(
            When(
                upcoming_q,
                then=Coalesce("event_date", "application_deadline"),
            ),
            default=Value(None, output_field=DateTimeField()),
            output_field=DateTimeField(),
        )

        # Buckets 0 + 3 only. A draft has no meaningful date of its own, so it
        # sorts by the edit that left it in this state.
        sort_recent = Case(
            When(status=Recruitment.Status.DRAFT, then=F("updated_at")),
            When(upcoming_q, then=Value(None, output_field=DateTimeField())),
            default=Coalesce(
                "trial_end_date", "event_date", "published_at"
            ),
            output_field=DateTimeField(),
        )

        return queryset.annotate(
            list_bucket=bucket,
            sort_upcoming=sort_upcoming,
            sort_recent=sort_recent,
        ).order_by(
            "list_bucket",
            F("sort_upcoming").asc(nulls_last=True),
            F("sort_recent").desc(nulls_last=True),
            "-published_at",
            "-created_at",
        )

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
        now=None,
    ):
        """
        The filtered candidate set — no ordering, no slicing, no prefetch.

        Split out of ``list_recruitments`` because the ranked "All" tab orders
        by a score computed in Python (§3) and therefore cannot let SQL do the
        LIMIT. Everything that decides WHICH rows are visible lives here, so
        both orderings answer over exactly the same set.

        ``now`` only feeds the trial-over rule; it exists so tests can pin the
        clock. Callers leave it None.
        """

        queryset = Recruitment.objects.filter(
            is_deleted=False,
            # A suspended club's listings go with it. This decides which
            # recruitments are visible for the ranked list, search and the org
            # profile at once. Discover and the shortlist build their own
            # candidate sets and restate the rule (discover_candidates,
            # visible_to_actor_queryset).
            organization__is_suspended=False,
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

            # TRIAL OVER — a trial whose last day has ended AT ITS OWN
            # VENUE is gone from every player-facing list:
            # the All tab, the ranked list, search, and another org's profile
            # tab. Sits inside the non-owner branch on purpose: the owning
            # org's own list keeps ended trials, the same way it keeps drafts
            # and closed postings. The shortlist and My applications do not
            # come through here (see visible_to_actor_queryset), so a player
            # who saved or applied still finds the trial in their own lists.
            queryset = queryset.filter(trial_not_over_q(now))

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

        # CITY — the trial's own city, or any live centre's. A city tour is
        # geocoded at one stop, so ?city=Kannur used to miss a trial that
        # visits Kannur next Saturday. Same live-centre rule as the distance
        # annotation (live_session_q), so a centre that has already run does
        # not keep its city matching.
        if city:
            queryset = queryset.filter(
                Q(city__iexact=city)
                | (
                    live_session_q(now, prefix="sessions__")
                    & Q(sessions__city__iexact=city)
                )
            ).distinct()

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

        # BOOKMARK. Annotated on the candidate set itself so every consumer
        # of this queryset — the plain "All" tab, the ranked one, the org
        # profile — carries `is_saved` without a per-row lookup. One Exists
        # subquery, and Django drops it again from the .count() that
        # list_recruitments takes off the same queryset.
        queryset = SavedRecruitmentSelector.annotate_is_saved(queryset, actor)

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

        # DISTANCE, in two independent halves.
        #
        # ANNOTATING is unconditional on knowing where the viewer is, because
        # the CARD reads it: a card that says "Kochi" with no number, on a
        # trial whose nearest centre is six kilometres away, is the org public
        # profile's whole problem. It costs the correlated subquery and
        # nothing else — no row is added or dropped by annotating.
        #
        # FILTERING stays opt-in, and only when the viewer actually asked for
        # "within N km". It is the bounding box first (it uses the existing
        # (latitude, longitude) indexes), then the exact haversine — the same
        # two-step as ExploreService._players_queryset. Rows with no
        # coordinates drop out of a distance-FILTERED list, which is correct:
        # the viewer asked a question an unknown venue cannot answer. Scoring
        # treats the same unknown as neutral (+5) precisely because it is not
        # a filter.
        #
        # The order matters: the filter reads the annotation.
        if center:
            queryset = RecruitmentSelector.annotate_distance(
                queryset, center, now=now
            )
        if center and max_distance_km:
            queryset = RecruitmentSelector.filter_within_distance(
                queryset, center, max_distance_km, now=now
            )

        return queryset

    # ------------------------------------------------------------ #
    # DISTANCE (§3 / §4) — the trig itself lives in services.geo
    # ------------------------------------------------------------ #

    # ONE CORRELATED SUBQUERY PER ROW, and that is the intended trade. It
    # keeps the whole thing a single round trip, and it reads the
    # (latitude, longitude) index on recruitment_trial_sessions that stage 1
    # added. At this project's scale — a corpus meant to stay in the low
    # thousands (§1), bounded by MAX_SCORED_CANDIDATES — that is cheaper than
    # any alternative that keeps the answer correct. If it ever does become
    # slow, the next step is a flat search-points table (one row per centre,
    # plus one per recruitment with none) or PostGIS, NOT a rewrite of this:
    # the shape below is what those would replace, and the callers would not
    # change.
    @staticmethod
    def annotate_distance(queryset, center, now=None):
        """
        Add ``distance_km`` from ``center`` to the NEAREST live trial centre,
        falling back to the recruitment's own venue coordinates.

        A posting with centres in Kochi and Kannur is geocoded at ONE of them.
        Read off that single pin, the trial sat 280 km from a Kannur player
        who was 4 km from the ground it actually visits, so a "within 50 km"
        search hid it. The nearest centre is the only distance that answers
        the question the viewer asked.

        ONLY LIVE CENTRES COUNT — see ``live_session_q``. A Kochi date that
        has already run must stop making the trial "near Kochi"; its
        coordinates stay on the row (the applicants who picked it still need
        them) and simply stop being a reason to surface the posting.

        Also adds ``nearest_session_id`` — WHICH centre that was, so the card
        can name the place the number belongs to instead of the one venue the
        org happened to geocode.

        NOT filtered: discovery scores every candidate, and a row with no
        coordinates has to survive to collect its +5 neutral. Such a row
        annotates to NULL → ``distance_km is None`` in Python. Both Coalesce
        arms can be NULL and NULL is what comes out — there is deliberately no
        third arm, because a 0 fallback would read as "right here" and sort an
        unknown venue to the top of every nearby list.

        ``nearest_session_id`` IS NULL ON EXACTLY THE SAME CONDITION that
        makes ``distance_km`` fall through to the recruitment's own pin: no
        live centre with coordinates. So "a distance but no centre" is a real
        and normal state, and it means the number is measured to the trial's
        OWN venue — which is what the card must then name. The two annotations
        are derived from one queryset below so they can never disagree about
        which centres were live.
        """
        lat, lng = center

        # The live centres we know the position of, nearest first. ONE base
        # queryset for both subqueries: two copies of this filter would be two
        # chances for the distance and the id to describe different rows.
        #
        # ``order_by("d")`` is not optional: the model has a Meta.ordering,
        # and leaving it in would both order the subquery by date and drag its
        # columns into a single-column SELECT.
        nearest_centres = (
            TrialSession.objects
            .filter(
                live_session_q(now),
                recruitment=OuterRef("pk"),
                latitude__isnull=False,
                longitude__isnull=False,
            )
            .annotate(
                d=haversine.distance_expr(
                    lat, lng, "latitude", "longitude"
                )
            )
            .order_by("d")
        )

        return queryset.annotate(
            distance_km=Coalesce(
                # No live centre with coordinates → no rows → NULL → fall
                # through to the trial's own pin.
                Subquery(
                    nearest_centres.values("d")[:1],
                    output_field=FloatField(),
                ),
                haversine.distance_expr(
                    lat, lng, "latitude", "longitude"
                ),
            ),
            nearest_session_id=Subquery(
                nearest_centres.values("id")[:1],
                output_field=UUIDField(),
            ),
        )

    @staticmethod
    def filter_within_distance(queryset, center, radius_km, now=None):
        """
        Box prefilter + exact circle. Expects ``annotate_distance`` first.

        The box is an OR now: the trial's own point inside it, or ANY live
        centre's. Reading the recruitment's pin alone dropped a posting whose
        own point is far away while one of its centres is next door — the
        exact row ``annotate_distance`` was changed to measure correctly, so
        a box that disagreed would throw it away before the circle ever saw
        it.

        Still only a prefilter. ``distance_km`` is the correctness step and it
        is unchanged: the box lets Postgres use the two coordinate indexes
        instead of running the trig over the whole table, and a corner of the
        box is further than its radius.
        """
        lat, lng = center
        box = haversine.bounding_box(lat, lng, radius_km)

        own_point_in_box = Q(
            latitude__gte=box["min_lat"],
            latitude__lte=box["max_lat"],
            longitude__gte=box["min_lng"],
            longitude__lte=box["max_lng"],
        )
        # ONE filter() call, so every condition lands on the SAME joined
        # session row — chained calls would let one centre be live and a
        # different one be in the box.
        centre_in_box = live_session_q(now, prefix="sessions__") & Q(
            sessions__latitude__gte=box["min_lat"],
            sessions__latitude__lte=box["max_lat"],
            sessions__longitude__gte=box["min_lng"],
            sessions__longitude__lte=box["max_lng"],
        )

        return queryset.filter(
            own_point_in_box | centre_in_box,
            distance_km__lte=radius_km,
        # A trial with two centres in range matches the join twice.
        ).distinct()

    # ------------------------------------------------------------ #
    # DISCOVER (§4)
    # ------------------------------------------------------------ #

    @staticmethod
    def discover_candidates(context, followed_org_ids, now=None, actor=None):
        """
        Every recruitment the discover sections may rank: active, live, visible
        to this viewer, and still open.

        ``actor`` is only ever read for the bookmark annotation — the
        VISIBILITY answer comes from ``context``/``followed_org_ids``, which
        the caller has already resolved.

        Two deliberate differences from the "All" tab's candidate set:

          - followers-only postings from orgs this viewer follows ARE included.
            ``list_recruitments`` only widens past PUBLIC when it is scoped to
            one org's profile; here the follow set is already resolved for the
            +10 signal, so honouring the visibility rule costs nothing.
          - deadline-passed rows are excluded. They stay in "All" for badging
            (§4); a section called "Recommended for you" that opens with a trial
            that closed last week is not a recommendation.

        Ended trials (``trial_not_over_q``) and suspended organizations are
        excluded here too. Discover builds its own candidate set and never
        passes through ``build_list_queryset``, so neither rule is inherited
        from there — both have to be restated.

        The payload this feeds is cached per actor for CACHE_TTL_SECONDS (ten
        minutes), so a trial can linger in a cached page for up to that long
        after midnight at its venue. Accepted: it is the same tolerance the
        cache already grants a deadline that passes mid-window.
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
            organization__is_suspended=False,
        ).filter(
            Q(application_deadline__isnull=True)
            | Q(application_deadline__gte=now)
        ).filter(
            trial_not_over_q(now)
        )

        if context.center:
            queryset = RecruitmentSelector.annotate_distance(
                queryset, context.center, now=now
            )

        # Same bookmark the "All" tab carries — the rails render the same card.
        queryset = SavedRecruitmentSelector.annotate_is_saved(queryset, actor)

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
            .prefetch_related(
                "questions__options", "age_categories", "sessions__location"
            )
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
        # BOOKMARK — one Exists subquery on the row we were fetching anyway, so
        # the detail page's bookmark starts filled without a second request.
        queryset = SavedRecruitmentSelector.annotate_is_saved(queryset, actor)
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
            "sessions__location",
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
    # ------------------------------------------------------------ #
    # SAVED (shortlist)
    # ------------------------------------------------------------ #

    @staticmethod
    def visible_to_actor_queryset(actor):
        """
        Every recruitment ``actor`` is still allowed to SEE, regardless of
        status.

        The visibility clause is the same one ``discover_candidates`` builds —
        public, plus followers-only from an org the viewer follows, plus the
        viewer's own listings when acting as the org — with the status and
        deadline gates deliberately left off. That is the one difference the
        shortlist needs: a saved trial that closed last week must stay in the
        list wearing its "Closed" badge, and ``build_list_queryset`` cannot
        answer that (it pins non-owners to ACTIVE, which is right for a
        discovery list and wrong for a shortlist). The trial-over rule is
        left off for the same reason: an ended trial stays on the shortlist
        wearing ``is_trial_over``, because the shortlist is the player's own.

        Kept in this module, not in the saved-list view, so visibility keeps
        having exactly one home.
        """
        followed_org_ids = FollowService.get_following_ids(actor)["org_ids"]

        visibility = Q(visibility=Recruitment.Visibility.PUBLIC)

        if followed_org_ids:
            visibility |= Q(
                visibility=Recruitment.Visibility.FOLLOWERS_ONLY,
                organization_id__in=followed_org_ids,
            )

        # An org actor keeps seeing its own postings at every status and
        # visibility — same owner escape hatch build_list_queryset applies.
        if actor and actor.is_org:
            visibility |= Q(organization_id=actor.organization.id)

        return Recruitment.objects.filter(
            visibility,
            is_deleted=False,
            organization__is_suspended=False,
        )
