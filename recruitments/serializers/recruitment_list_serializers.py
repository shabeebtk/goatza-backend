# recruitments/serializers/recruitment_list_serializers.py
from rest_framework import serializers
from recruitments.models import (
    Recruitment, RecruitmentMedia, RecruitmentQuestion,
    RecruitmentQuestionOption, RecruitmentApplication, RecruitmentPosition,
    RecruitmentAgeCategory, RecruitmentContact, RecruitmentBenefit, RecruitmentRequirement,
    RecruitmentEligibilityCriteria
)
from organization.serializers.organization_serializers import OrganizationMiniSerializer
from sports.serializers.sports_serializers import SportSerializer, SportPositionSerializer


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


class RecruitmentListSerializer(serializers.ModelSerializer):
    organization = OrganizationMiniSerializer(read_only=True)
    sport = SportSerializer(read_only=True)
    positions = RecruitmentPositionMiniSerializer(many=True, read_only=True)
    cover_media = serializers.SerializerMethodField()
    # The list selector already prefetches age_categories, so the card's age
    # chip costs no extra query. An empty list means "open to all ages".
    age_categories = RecruitmentAgeCategorySerializer(many=True, read_only=True)

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
        ]

    def get_cover_media(self, obj):

        first_media = next(iter(obj.media.all()), None)

        if not first_media:
            return None

        return {
            "media_type": first_media.media_type,
            "file_url": first_media.file_url,
            "thumbnail_url": first_media.thumbnail_url,
        }
    



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

    class Meta:
        model = RecruitmentApplication

        fields = [
            "id",
            "status",
            "applied_at",
            "updated_at",
            "age_category",
        ]


# PUBLIC DETAIL SERIALIZER
class RecruitmentDetailSerializer(serializers.ModelSerializer):
    organization = OrganizationMiniSerializer(read_only=True)
    sport = SportSerializer(read_only=True)
    positions = RecruitmentPositionMiniSerializer(many=True, read_only=True)
    media = RecruitmentMediaSerializer(many=True, read_only=True)
    questions = RecruitmentQuestionSerializer(many=True, read_only=True)
    my_application = serializers.SerializerMethodField()
    can_apply = serializers.SerializerMethodField()
    is_accepting_applications = serializers.BooleanField(read_only=True)
    age_categories = RecruitmentAgeCategorySerializer(many=True, read_only=True)
    contacts = RecruitmentContactSerializer(many=True, read_only=True)
    benefits = RecruitmentBenefitSerializer(many=True, read_only=True)
    requirements = RecruitmentRequirementSerializer(many=True, read_only=True)
    eligibility_criteria = RecruitmentEligibilityCriteriaSerializer(
        many=True, read_only=True
    )

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
            "external_apply_url",

            "created_at",
        ]

    # PLAYER APPLICATION
    def get_my_application(self, obj):
        request = self.context.get("request")
        actor = getattr(request, "actor", None)

        if not actor or not actor.is_user:
            return None

        application = obj.applications.select_related(
            "age_category"
        ).filter(
            applicant=actor.user
        ).first()

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


# OWNER DETAIL SERIALIZER
class RecruitmentOwnerDetailSerializer(
    RecruitmentDetailSerializer
):

    class Meta(RecruitmentDetailSerializer.Meta):

        fields = RecruitmentDetailSerializer.Meta.fields + [
            "status",

            "max_applications",

            "shortlisted_count",
            "selected_count",

            "views_count",

            "published_at",
            "updated_at",
        ]