# recruitments/serializers/recruitment_list_serializers.py
from rest_framework import serializers
from recruitments.models import (
    Recruitment, RecruitmentMedia, RecruitmentQuestion, 
    RecruitmentQuestionOption, RecruitmentApplication, RecruitmentPosition
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


class RecruitmentListSerializer(serializers.ModelSerializer):
    organization = OrganizationMiniSerializer(read_only=True)
    sport = SportSerializer(read_only=True)
    positions = RecruitmentPositionMiniSerializer(many=True, read_only=True)
    cover_media = serializers.SerializerMethodField()

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
    



class RecruitmentMediaSerializer(serializers.ModelSerializer):

    class Meta:
        model = RecruitmentMedia

        fields = [
            "id",
            "media_type",
            "file_url",
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

    class Meta:
        model = RecruitmentApplication

        fields = [
            "id",
            "status",
            "applied_at",
            "updated_at",
        ]


# PUBLIC DETAIL SERIALIZER
class RecruitmentDetailSerializer(serializers.ModelSerializer):

    organization = OrganizationMiniSerializer(
        read_only=True
    )

    sport = SportSerializer(
        read_only=True
    )

    positions = RecruitmentPositionMiniSerializer(
        many=True,
        read_only=True
    )

    media = RecruitmentMediaSerializer(
        many=True,
        read_only=True
    )

    questions = RecruitmentQuestionSerializer(
        many=True,
        read_only=True
    )

    my_application = serializers.SerializerMethodField()

    can_apply = serializers.SerializerMethodField()

    class Meta:
        model = Recruitment

        fields = [
            "id",

            "title",
            "short_description",
            "description",

            "recruitment_type",
            "visibility",

            "gender",

            "min_age",
            "max_age",

            "experience_level",

            "application_deadline",
            "event_date",

            "is_remote",

            "is_paid",
            "fee_amount",
            "fee_currency",
            "payment_note",

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

            "my_application",
            "can_apply",

            "created_at",
        ]

    # PLAYER APPLICATION
    def get_my_application(self, obj):

        request = self.context.get("request")

        actor = getattr(request, "actor", None)

        if not actor or not actor.is_user:
            return None

        application = obj.applications.filter(
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

        if obj.status != Recruitment.Status.ACTIVE:
            return False

        if obj.application_deadline:
            from django.utils import timezone

            if timezone.now() > obj.application_deadline:
                return False

        already_applied = obj.applications.filter(
            applicant=actor.user
        ).exists()

        return not already_applied


# OWNER DETAIL SERIALIZER
class RecruitmentOwnerDetailSerializer(
    RecruitmentDetailSerializer
):

    class Meta(RecruitmentDetailSerializer.Meta):

        fields = RecruitmentDetailSerializer.Meta.fields + [

            "status",

            "shortlisted_count",
            "selected_count",

            "views_count",

            "published_at",
            "updated_at",
        ]