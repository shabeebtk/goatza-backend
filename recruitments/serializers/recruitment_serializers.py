from django.utils import timezone
from rest_framework import serializers
from recruitments.models import (
    Recruitment,
    RecruitmentQuestion
)
from sports.models import SportPosition, Sport

# POSITION INPUT
class RecruitmentPositionInputSerializer(serializers.Serializer):
    position_id = serializers.UUIDField()
    is_primary = serializers.BooleanField(default=False)


# QUESTION OPTION INPUT
class RecruitmentQuestionOptionInputSerializer(serializers.Serializer):
    value = serializers.CharField(max_length=255)


# QUESTION INPUT
class RecruitmentQuestionInputSerializer(serializers.Serializer):
    question = serializers.CharField(max_length=255)
    field_type = serializers.ChoiceField(
        choices=RecruitmentQuestion.FieldType.choices
    )
    is_required = serializers.BooleanField(default=False)
    placeholder = serializers.CharField(
        required=False,
        allow_blank=True
    )
    help_text = serializers.CharField(
        required=False,
        allow_blank=True
    )
    options = RecruitmentQuestionOptionInputSerializer(
        many=True,
        required=False
    )

    def validate(self, attrs):

        field_type = attrs.get("field_type")
        options = attrs.get("options", [])

        option_required_types = [
            RecruitmentQuestion.FieldType.SELECT,
            RecruitmentQuestion.FieldType.RADIO,
            RecruitmentQuestion.FieldType.CHECKBOX,
        ]

        # option-required fields
        if field_type in option_required_types and not options:
            raise serializers.ValidationError(
                "Options are required for select/radio/checkbox fields."
            )

        # text/number fields should not contain options
        if (
            field_type not in option_required_types
            and options
        ):
            raise serializers.ValidationError(
                "Options are not allowed for this field type."
            )

        return attrs


# MEDIA INPUT
class RecruitmentMediaInputSerializer(serializers.Serializer):
    file_url = serializers.URLField()
    public_id = serializers.CharField(max_length=255)
    media_type = serializers.ChoiceField(
        choices=["image", "video"]
    )
    thumbnail_url = serializers.URLField(
        required=False,
        allow_blank=True
    )
    duration = serializers.IntegerField(
        required=False,
        min_value=1
    )
    order = serializers.IntegerField(default=0)


# LOCATION INPUT
class RecruitmentLocationInputSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=255)
    city = serializers.CharField(max_length=100)
    country_code = serializers.CharField(max_length=5)
    latitude = serializers.FloatField()
    longitude = serializers.FloatField()


# CREATE RECRUITMENT SERIALIZER
class RecruitmentCreateSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=255)
    short_description = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=300
    )
    description = serializers.CharField(
        required=False,
        allow_blank=True
    )
    recruitment_type = serializers.ChoiceField(
        choices=Recruitment.Type.choices
    )
    visibility = serializers.ChoiceField(
        choices=Recruitment.Visibility.choices,
        default=Recruitment.Visibility.PUBLIC
    )
    gender = serializers.ChoiceField(
        choices=Recruitment.Gender.choices,
        required=False
    )
    sport_id = serializers.UUIDField()
    min_age = serializers.IntegerField(
        required=False,
        min_value=5,
        max_value=100
    )
    max_age = serializers.IntegerField(
        required=False,
        min_value=5,
        max_value=100
    )
    experience_level = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=50
    )
    application_deadline = serializers.DateTimeField(
        required=False
    )
    event_date = serializers.DateTimeField(
        required=False
    )
    is_remote = serializers.BooleanField(default=False)
    max_applications = serializers.IntegerField(
        required=False,
        min_value=1
    )
    # fee
    is_paid = serializers.BooleanField(default=False)
    fee_amount = serializers.DecimalField(
        max_digits=10,
        decimal_places=2,
        required=False
    )
    fee_currency = serializers.CharField(
        required=False,
        default="INR"
    )
    payment_note = serializers.CharField(
        required=False,
        allow_blank=True
    )

    # nested
    location = RecruitmentLocationInputSerializer(
        required=False
    )

    positions = RecruitmentPositionInputSerializer(
        many=True
    )

    questions = RecruitmentQuestionInputSerializer(
        many=True,
        required=False
    )

    media = RecruitmentMediaInputSerializer(
        many=True,
        required=False
    )

    # VALIDATIONS
    def validate_sport_id(self, value):
        sport = Sport.objects.filter(id=value).only("id").first()
        if not sport:
            raise serializers.ValidationError("Invalid sport_id")
        return value

    def validate_positions(self, value):
        if not value:
            raise serializers.ValidationError(
                "At least one position is required."
            )
        position_ids = [str(v["position_id"]) for v in value]
        if len(position_ids) != len(set(position_ids)):
            raise serializers.ValidationError(
                "Duplicate positions are not allowed."
            )
        return value

    def validate(self, attrs):
        sport_id = attrs.get("sport_id")
        min_age = attrs.get("min_age")
        max_age = attrs.get("max_age")
        is_paid = attrs.get("is_paid")
        fee_amount = attrs.get("fee_amount")
        event_date = attrs.get("event_date")
        application_deadline = attrs.get("application_deadline")
        positions = attrs.get("positions", [])

        # AGE VALIDATION
        if (
            min_age is not None
            and max_age is not None
            and min_age > max_age
        ):
            raise serializers.ValidationError(
                "min_age cannot be greater than max_age"
            )

        # PAYMENT VALIDATION
        if is_paid and not fee_amount:
            raise serializers.ValidationError(
                "fee_amount is required for paid recruitments"
            )

        if not is_paid and fee_amount:
            raise serializers.ValidationError(
                "fee_amount should be empty for free recruitments"
            )

        # DATE VALIDATION
        now = timezone.now()

        if application_deadline and application_deadline < now:
            raise serializers.ValidationError(
                "Application deadline cannot be in the past"
            )

        if (
            application_deadline
            and event_date
            and application_deadline > event_date
        ):
            raise serializers.ValidationError(
                "Application deadline cannot be after event date"
            )

        # POSITION VALIDATION
        db_positions = SportPosition.objects.filter(
            id__in=[p["position_id"] for p in positions]
        ).select_related("sport")

        db_position_map = {
            str(p.id): p
            for p in db_positions
        }

        for position_data in positions:
            position_id = str(position_data["position_id"])
            position = db_position_map.get(position_id)

            if not position:
                raise serializers.ValidationError(
                    f"Invalid position_id: {position_id}"
                )

            if str(position.sport_id) != str(sport_id):
                raise serializers.ValidationError(
                    f"Position '{position.name}' "
                    f"does not belong to selected sport."
                )

        # PRIMARY POSITION VALIDATION
        primary_count = sum(
            1 for p in positions if p.get("is_primary")
        )

        if primary_count > 1:
            raise serializers.ValidationError(
                "Only one primary position is allowed."
            )

        return attrs