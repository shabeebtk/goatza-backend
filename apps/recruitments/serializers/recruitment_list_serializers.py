# recruitments/serializers/recruitment_list_serializers.py
from datetime import time

from rest_framework import serializers
from apps.recruitments.models import (
    Recruitment, RecruitmentMedia, RecruitmentQuestion,
    RecruitmentQuestionOption, RecruitmentApplication, RecruitmentPosition,
    RecruitmentAgeCategory, RecruitmentContact, RecruitmentBenefit, RecruitmentRequirement,
    RecruitmentEligibilityCriteria
)
from apps.organization.serializers.organization_serializers import OrganizationMiniSerializer
from apps.sports.serializers.sports_serializers import SportSerializer, SportPositionSerializer
from apps.recruitments.feedback_window import (
    can_give_feedback,
    prompt_window_open,
)


class RecruitmentPositionMiniSerializer(serializers.ModelSerializer):

    position = SportPositionSerializer(read_only=True)

    class Meta:
        model = RecruitmentPosition
        fields = [
            "position",
            "is_primary"
        ]


class RecruitmentAgeCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = RecruitmentAgeCategory

        fields = [
            "id",
            "title",
            "min_birth_year",
            "max_birth_year",
            "reporting_time",
        ]

    
# The age group ON AN APPLICATION — the slice both sides need: which group the
# applicant chose, and when that group reports. The birth-year range belongs to
# the recruitment's own age_categories list, not to the application row.
class ApplicationAgeCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = RecruitmentAgeCategory

        fields = [
            "id",
            "title",
            "reporting_time",
        ]


class RecruitmentContactSerializer(serializers.ModelSerializer):
    class Meta:
        model = RecruitmentContact

        fields = [
            "id",
            "name",
            "contact_type",
            "value",
        ]


class RecruitmentBenefitSerializer(serializers.ModelSerializer):
    class Meta:
        model = RecruitmentBenefit

        fields = [
            "id",
            "title",
            "icon_name",
        ]


class RecruitmentRequirementSerializer(serializers.ModelSerializer):
    class Meta:
        model = RecruitmentRequirement
        fields = [
            "id",
            "title",
            "is_mandatory",
        ]


class RecruitmentEligibilityCriteriaSerializer(serializers.ModelSerializer):
    class Meta:
        model = RecruitmentEligibilityCriteria
        fields = [
            "id",
            "title",
        ]


# =========================================================
# TRIAL SESSIONS
# =========================================================
# Not a ModelSerializer: every venue field on a session is an OVERRIDE that
# falls back to the recruitment's, so the payload has to be built with the
# parent in hand. Doing it as a nested ModelSerializer would mean reaching
# for ``session.recruitment`` once per row — a query per date on every card.

def trial_session_payload(session, recruitment):
    """
    One date, with its venue resolved: the session's own value where it set
    one, the recruitment's where it did not.
    """
    return {
        "id": str(session.id),
        "title": session.title,
        "date": session.date,
        "start_time": session.start_time,
        "end_time": session.end_time,
        "is_cancelled": session.is_cancelled,
        "venue_name": session.venue_name or recruitment.venue_name,
        "venue_link": session.venue_link or recruitment.venue_link,
        "city": session.city or recruitment.city,
        "latitude": (
            session.latitude
            if session.latitude is not None
            else recruitment.latitude
        ),
        "longitude": (
            session.longitude
            if session.longitude is not None
            else recruitment.longitude
        ),
    }


def trial_sessions_payload(recruitment):
    """
    Every date on a recruitment: live ones first, each group in the model's
    own ordering (date, start time, display order). Sorted in Python off the
    prefetched rows, so this costs no query.
    """
    sessions = sorted(
        recruitment.sessions.all(),
        key=lambda session: (
            session.is_cancelled,
            session.date,
            session.start_time or time(23, 59),
            session.display_order,
        ),
    )
    return [
        trial_session_payload(session, recruitment)
        for session in sessions
    ]


# The chosen date ON AN APPLICATION — the slice both sides need. Null unless
# the trial is choose_one, because nothing else has a date to pick.
def application_session_payload(application):
    session = application.session
    if session is None:
        return None

    resolved = trial_session_payload(session, application.recruitment)
    return {
        key: resolved[key]
        for key in (
            "id", "title", "date", "start_time",
            "venue_name", "venue_link", "city",
        )
    }


class TrialSessionsMixin(metaclass=serializers.SerializerMetaclass):
    """
    The trial-window fields every recruitment payload carries, plus the dates
    themselves.

    ``event_date`` stays exactly where it already is on each payload — it is
    still the first session and a lot of the client reads it. What is new is
    that ``applications_close_at`` is NOT the same instant: on an "attend
    every date" trial applications close on day one while the trial runs on.

    ``timezone`` is here because the client CANNOT FORMAT ANY OF THE OTHERS
    CORRECTLY WITHOUT IT. A trial's instants are stored for its own country,
    so rendering them on the viewer's clock is how a player in Dubai reads a
    London trial as starting at 1pm. It is also the zone the wizard's date
    input interprets typed times in, and what decides whether a time needs a
    "(London)" suffix beside it. Carried on a player_looking post too, where
    there is no trial to format: one field on every payload beats a field
    that is sometimes there.

    THE METACLASS IS LOAD-BEARING. DRF collects declared fields off a base
    class only when that base carries ``_declared_fields``, which only
    ``SerializerMetaclass`` puts there. A plain mixin's fields are silently
    dropped — and ``sessions`` does not then go missing, which would have been
    caught in a minute: ModelSerializer sees the reverse relation and builds a
    ``PrimaryKeyRelatedField(many=True)``, so every payload shipped a list of
    bare session UUIDs where the client expected date objects.
    """

    sessions = serializers.SerializerMethodField()
    session_mode = serializers.CharField(read_only=True)
    timezone = serializers.CharField(read_only=True)
    trial_end_date = serializers.DateTimeField(read_only=True)
    applications_close_at = serializers.DateTimeField(read_only=True)

    auto_confirm = serializers.BooleanField(read_only=True)

    SESSION_FIELDS = [
        "sessions",
        "session_mode",
        # The venue's calendar. Every date and time on this payload is an
        # instant resolved in it — see the class docstring.
        "timezone",
        "trial_end_date",
        "applications_close_at",
        # An open-trial-only setting, and a public one: a player deciding
        # whether to apply wants to know a place is guaranteed rather than
        # screened.
        "auto_confirm",
    ]

    def get_sessions(self, obj):
        return trial_sessions_payload(obj)


class RecruitmentListSerializer(
    TrialSessionsMixin, serializers.ModelSerializer
):
    organization = OrganizationMiniSerializer(read_only=True)
    sport = SportSerializer(read_only=True)
    positions = RecruitmentPositionMiniSerializer(many=True, read_only=True)
    cover_media = serializers.SerializerMethodField()
    # How many photos there are, so the card's media slot can badge "1/4".
    # Counted off the ALREADY-PREFETCHED list, never a second query — see
    # LIST_PREFETCH_RELATED. `.count()` here would be one query per row.
    media_count = serializers.SerializerMethodField()
    # The list selector already prefetches age_categories, so the card's age
    # chip costs no extra query. An empty list means "open to all ages".
    age_categories = RecruitmentAgeCategorySerializer(many=True, read_only=True)
    is_saved = serializers.SerializerMethodField()
    # A model property over event_date — no query. Player-facing lists never
    # contain an ended trial, so there it is always false; it matters on the
    # surfaces that KEEP ended trials (the owner's list, the shortlist, a
    # direct link) so the card can wear a "Trial over" badge.
    is_trial_over = serializers.BooleanField(read_only=True)

    class Meta:

        model = Recruitment

        fields = [
            "id",
            "title",
            "short_description",
            "recruitment_type",
            "status",
            "visibility",
            "city",
            "applications_count",
            "event_date",
            "created_at",
            "organization",
            "sport",
            "positions",
            "cover_media",
            "media_count",
            "age_categories",
            # The card's deadline countdown and its fee cell. Both are plain
            # columns on the row the selector already fetches, so neither adds
            # a query — and a card that cannot say "closes in 3 days" or
            # "Free vs ₹200" is missing the two facts a player decides on.
            "application_deadline",
            "is_paid",
            "fee_amount",
            "fee_currency",
            # Venue beats city: "Corporation Stadium" locates a trial, and
            # "Kozhikode" only narrows it to a district.
            "venue_name",
            # Shipped for the detail page and future filters. The card
            # deliberately does NOT render it — a third value in the age/fee
            # cell costs more scannability than the signal is worth.
            "gender",
            # The bookmark. Always present so the card never has to guess.
            "is_saved",
            "is_trial_over",
        ] + TrialSessionsMixin.SESSION_FIELDS

    def get_is_saved(self, obj):
        """
        Whether the CURRENT actor shortlisted this recruitment.

        Supplied by SavedRecruitmentSelector.annotate_is_saved on every
        queryset that reaches this serializer. The default keeps an
        un-annotated path rendering an empty bookmark instead of raising — but
        that is a bug at the call site, not a feature, and it is also what
        makes the serializer safe to reuse on the anonymous public org profile.
        """
        return bool(getattr(obj, "is_saved", False))

    def get_cover_media(self, obj):

        first_media = next(iter(obj.media.all()), None)

        if not first_media:
            return None

        return {
            "media_type": first_media.media_type,
            "file_url": first_media.file_url,
            "thumbnail_url": first_media.thumbnail_url,
        }

    def get_media_count(self, obj):
        return len(obj.media.all())
    



class RecruitmentDiscoverItemSerializer(RecruitmentListSerializer):
    """
    A ranked card (§4/§5). The list card plus the match context behind its
    position.

    The score itself IS in the payload but the card never renders it — §5 is
    explicit that a number invites argument while a reason builds trust, so the
    client draws chips from ``sport_match`` / ``position_match`` /
    ``distance_km`` / ``days_to_deadline`` instead. It ships anyway because
    ordering is only debuggable if the number is visible somewhere.

    ``application_deadline`` used to be declared here on the argument that only
    a ranked card needed it. That stopped being true once the card grew a
    deadline countdown: the org public profile renders the SAME card off the
    plain list serializer, and it had no deadline data to count down from. It
    now lives on the parent and is inherited.

    ``published_at`` stays here — it is genuinely discover-only ("New this
    week" sorts on it) and nothing on a card reads it.
    """

    match_score = serializers.SerializerMethodField()
    is_eligible = serializers.SerializerMethodField()
    eligibility_badge = serializers.SerializerMethodField()
    sport_match = serializers.SerializerMethodField()
    position_match = serializers.SerializerMethodField()
    matched_positions = serializers.SerializerMethodField()
    distance_km = serializers.SerializerMethodField()
    days_to_deadline = serializers.SerializerMethodField()

    class Meta(RecruitmentListSerializer.Meta):
        fields = RecruitmentListSerializer.Meta.fields + [
            "published_at",
            "match_score",
            "is_eligible",
            "eligibility_badge",
            "sport_match",
            "position_match",
            "matched_positions",
            "distance_km",
            "days_to_deadline",
        ]

    # The scorer stamps its MatchResult onto the instance (see
    # RecruitmentDiscoverService); a row that somehow arrives unscored
    # serializes as "no match context" rather than blowing up the page.
    @staticmethod
    def _match(obj):
        return getattr(obj, "match", None)

    def get_match_score(self, obj):
        match = self._match(obj)
        return match.score if match else None

    def get_is_eligible(self, obj):
        match = self._match(obj)
        return match.is_eligible if match else True

    def get_eligibility_badge(self, obj):
        match = self._match(obj)
        return match.badge if match else None

    def get_sport_match(self, obj):
        match = self._match(obj)
        return match.sport_match if match else None

    def get_position_match(self, obj):
        match = self._match(obj)
        return match.position_match if match else None

    def get_matched_positions(self, obj):
        match = self._match(obj)
        return list(match.matched_positions) if match else []

    def get_distance_km(self, obj):
        match = self._match(obj)
        return match.distance_km if match else None

    def get_days_to_deadline(self, obj):
        match = self._match(obj)
        return match.days_to_deadline if match else None


class SavedRecruitmentListSerializer(RecruitmentListSerializer):
    """
    A shortlisted card: the ordinary list card plus WHEN it was saved.

    Deliberately flat and deliberately the same card — nesting the recruitment
    under a save row would have bought the frontend a second shape to render
    and a second card component to keep in step.

    ``saved_at`` comes off the save row the view stamps onto each instance
    (the same trick RecruitmentDiscoverItemSerializer uses for ``match``): the
    ordering lives on the save, so the timestamp has to come from there too.
    """

    saved_at = serializers.SerializerMethodField()

    class Meta(RecruitmentListSerializer.Meta):
        fields = RecruitmentListSerializer.Meta.fields + ["saved_at"]

    def get_saved_at(self, obj):
        return getattr(obj, "saved_at", None)


class RecruitmentMediaSerializer(serializers.ModelSerializer):

    class Meta:
        model = RecruitmentMedia

        fields = [
            "id",
            "media_type",
            "file_url",
            "public_id",
            "thumbnail_url",
            "duration",
            "order",
        ]


# =========================================================
# QUESTION OPTION
# =========================================================

class RecruitmentQuestionOptionSerializer(
    serializers.ModelSerializer
):

    class Meta:
        model = RecruitmentQuestionOption

        fields = [
            "id",
            "value",
        ]


# QUESTION
class RecruitmentQuestionSerializer(
    serializers.ModelSerializer
):
    options = RecruitmentQuestionOptionSerializer(
        many=True,
        read_only=True
    )

    class Meta:
        model = RecruitmentQuestion

        fields = [
            "id",
            "question",
            "field_type",
            "is_required",
            "placeholder",
            "help_text",
            "options",
        ]


# PLAYER APPLICATION
class MyApplicationSerializer(serializers.ModelSerializer):
    age_category = ApplicationAgeCategorySerializer(read_only=True)
    # The date they picked, on a choose_one trial. Null everywhere else.
    session = serializers.SerializerMethodField()
    # THE SAME ANSWERS My applications returns, because a player who opens
    # the trial they just attended is exactly who should be asked — and the
    # rule for whether to ask has one home (feedback_window.py), not two.
    # get_my_application attaches the recruitment it already has, so neither
    # flag walks the FK back.
    can_give_feedback = serializers.SerializerMethodField()
    feedback_window_open = serializers.SerializerMethodField()

    class Meta:
        model = RecruitmentApplication

        fields = [
            "id",
            "status",
            "applied_at",
            "updated_at",
            "age_category",
            "session",
            "attended_self_reported",
            "outcome_self_reported",
            "trial_rating",
            "trial_feedback",
            "feedback_at",
            "can_give_feedback",
            "feedback_window_open",
        ]

    def get_session(self, obj):
        return application_session_payload(obj)

    def get_can_give_feedback(self, obj):
        return can_give_feedback(obj, obj.recruitment)

    def get_feedback_window_open(self, obj):
        return prompt_window_open(obj.recruitment)


# PUBLIC DETAIL SERIALIZER
class RecruitmentDetailSerializer(
    TrialSessionsMixin, serializers.ModelSerializer
):
    organization = OrganizationMiniSerializer(read_only=True)
    sport = SportSerializer(read_only=True)
    positions = RecruitmentPositionMiniSerializer(many=True, read_only=True)
    media = RecruitmentMediaSerializer(many=True, read_only=True)
    questions = RecruitmentQuestionSerializer(many=True, read_only=True)
    my_application = serializers.SerializerMethodField()
    can_apply = serializers.SerializerMethodField()
    is_accepting_applications = serializers.BooleanField(read_only=True)
    # Direct links (shares, notifications) still open an ended trial; this is
    # how the page knows to say so. Same property the card reads, no query.
    is_trial_over = serializers.BooleanField(read_only=True)
    age_categories = RecruitmentAgeCategorySerializer(many=True, read_only=True)
    contacts = RecruitmentContactSerializer(many=True, read_only=True)
    benefits = RecruitmentBenefitSerializer(many=True, read_only=True)
    requirements = RecruitmentRequirementSerializer(many=True, read_only=True)
    eligibility_criteria = RecruitmentEligibilityCriteriaSerializer(
        many=True, read_only=True
    )
    is_saved = serializers.SerializerMethodField()

    class Meta:
        model = Recruitment

        fields = [
            "id",

            "title",
            "short_description",
            "description",

            "recruitment_type",
            "visibility",
            "apply_method",

            "gender",

            "experience_level",

            "application_deadline",
            "event_date",

            "is_remote",

            "is_paid",
            "fee_amount",
            "fee_currency",
            "payment_note",

            "venue_name",
            "venue_link",
            "location_name",
            "city",
            "country_code",
            "latitude",
            "longitude",

            "applications_count",

            "organization",
            "sport",
            "positions",
            "media",
            "questions",
            "age_categories",
            "contacts",
            "benefits",
            "requirements",
            "eligibility_criteria",

            "my_application",
            "can_apply",
            "is_accepting_applications",
            "is_trial_over",
            "external_apply_url",
            # Same bookmark the card carries, so the detail page's toggle has
            # its initial state without a second request.
            "is_saved",

            "created_at",
        ] + TrialSessionsMixin.SESSION_FIELDS

    # BOOKMARK — annotated by SavedRecruitmentSelector.annotate_is_saved on
    # the detail selector's queryset; see RecruitmentListSerializer.get_is_saved.
    def get_is_saved(self, obj):
        return bool(getattr(obj, "is_saved", False))

    # PLAYER APPLICATION
    def get_my_application(self, obj):
        request = self.context.get("request")
        actor = getattr(request, "actor", None)

        if not actor or not actor.is_user:
            return None

        application = obj.applications.select_related(
            "age_category",
            # The chosen date resolves its venue against the recruitment, so
            # hand the serializer the one we already have rather than letting
            # it walk the FK back.
            "session",
        ).filter(
            applicant=actor.user
        ).first()
        if application is not None:
            application.recruitment = obj

        if not application:
            return None

        return MyApplicationSerializer(application).data

    # APPLY BUTTON STATE
    def get_can_apply(self, obj):
        request = self.context.get("request")
        actor = getattr(request, "actor", None)

        if not actor or not actor.is_user:
            return False

        if actor.user.role != "player":
            return False

        # Single source of truth for status + deadline + max-applications cap,
        # so the Apply button hides the moment the recruitment stops accepting
        # applications (e.g. the cap is hit) — mirrors the apply endpoint's gate.
        if not obj.is_accepting_applications:
            return False

        # A withdrawn application does NOT block re-applying — the apply endpoint
        # revives the same row. Only a live application hides the button.
        already_applied = obj.applications.filter(
            applicant=actor.user
        ).exclude(
            status=RecruitmentApplication.Status.WITHDRAWN
        ).exists()

        return not already_applied


# VIEWER DETAIL SERIALIZER
class RecruitmentViewerDetailSerializer(RecruitmentDetailSerializer):
    """
    The public detail plus one fact about the viewer: their OWN birth year, so
    the apply modal can warn about a mismatched age group without a second
    request.

    Used by the authenticated detail endpoint for every caller except the
    owning org. Never by /public/recruitments/<id>: that payload is anonymous
    and cacheable, and a birth year does not belong in a response any layer
    between us and the browser may keep.
    """

    viewer_birth_year = serializers.SerializerMethodField()

    class Meta(RecruitmentDetailSerializer.Meta):
        fields = RecruitmentDetailSerializer.Meta.fields + [
            "viewer_birth_year",
        ]

    def get_viewer_birth_year(self, obj):
        """None for an org actor, an anonymous caller, or no birthdate."""
        request = self.context.get("request")
        actor = getattr(request, "actor", None)

        if not actor or not actor.is_user:
            return None

        profile = getattr(actor.user, "profile", None)
        birthdate = getattr(profile, "birthdate", None)
        return birthdate.year if birthdate else None


# OWNER DETAIL SERIALIZER
class RecruitmentOwnerDetailSerializer(
    RecruitmentDetailSerializer
):
    """
    The public detail plus the numbers only the posting org may see.

    The owner check is NOT repeated here: RecruitmentDetailAPIView already
    decides between this serializer and the public one, so membership of this
    class IS the gate. Every field below inherits that gating for free — which
    is exactly why a new owner-only field belongs here and nowhere else.
    """

    saves_count = serializers.SerializerMethodField()
    rating_average = serializers.SerializerMethodField()
    rating_count = serializers.SerializerMethodField()

    class Meta(RecruitmentDetailSerializer.Meta):

        fields = RecruitmentDetailSerializer.Meta.fields + [
            "status",

            "max_applications",

            "confirmed_count",
            "selected_count",

            "views_count",
            # How many actors shortlisted this posting. An AGGREGATE only — the
            # shortlist itself stays private to the saver (SavedRecruitment),
            # so this says how many, never who.
            "saves_count",

            # How the players rated the trial. An AGGREGATE, owner-only:
            # an individual rating stays between the player and the org that
            # ran the trial. Null average until somebody rates.
            "rating_average",
            "rating_count",

            "published_at",
            "updated_at",
        ]

    def _rating_summary(self, obj):
        # ONE aggregate per row, not two: the two fields below are two views
        # of the same query. Keyed by row id rather than cached flat on the
        # serializer, so this stays correct if it is ever used with
        # many=True instead of on a single detail row.
        cache = getattr(self, "_rating_cache", None)
        if cache is None:
            cache = self._rating_cache = {}

        if obj.id not in cache:
            from apps.recruitments.services.trial_feedback_service import (
                TrialFeedbackService,
            )
            cache[obj.id] = TrialFeedbackService.rating_summary(obj)

        return cache[obj.id]

    def get_rating_average(self, obj):
        return self._rating_summary(obj)[0]

    def get_rating_count(self, obj):
        return self._rating_summary(obj)[1]

    def get_saves_count(self, obj):
        # A COUNT on the one row we already fetched, not an annotation on the
        # detail queryset: the public serializer shares that queryset, and
        # annotating there would make every viewer pay for a number only the
        # owner is ever shown.
        return obj.saved_by.count()