from django.utils import timezone
from rest_framework import serializers
from recruitments.models import (
    Recruitment,
    RecruitmentQuestion,
    RecruitmentContact,
)
from sports.models import SportPosition, Sport

# POSITION INPUT
class RecruitmentPositionInputSerializer(serializers.Serializer):
    position_id = serializers.UUIDField()
    is_primary = serializers.BooleanField(default=False)


# QUESTION OPTION INPUT
class RecruitmentQuestionOptionInputSerializer(serializers.Serializer):
    value = serializers.CharField(max_length=255)


# AGE CATEGORY INPUT
class RecruitmentAgeCategoryInputSerializer(
    serializers.Serializer
):

    title = serializers.CharField(max_length=50)
    min_birth_year = serializers.IntegerField(min_value=1950)
    max_birth_year = serializers.IntegerField(min_value=1950)
    reporting_time = serializers.TimeField(required=False)
    display_order = serializers.IntegerField(default=0)

    def validate(self, attrs):

        if (
            attrs["min_birth_year"]
            > attrs["max_birth_year"]
        ):
            raise serializers.ValidationError(
                "Invalid birth year range."
            )

        return attrs
    

# CONTACT INPUT
class RecruitmentContactInputSerializer(serializers.Serializer):
    name = serializers.CharField(required=False, allow_blank=True)
    contact_type = serializers.ChoiceField(
        choices=RecruitmentContact.ContactType.choices
    )
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


# BENEFIT INPUT
class RecruitmentBenefitInputSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=255)
    icon_name = serializers.CharField(required=False, allow_blank=True)
    display_order = serializers.IntegerField(default=0)


# REQUIREMENT INPUT
class RecruitmentRequirementInputSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=255)
    is_mandatory = serializers.BooleanField(default=True)
    display_order = serializers.IntegerField(default=0)

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
    apply_method = serializers.ChoiceField(
        choices=Recruitment.ApplyMethod.choices,
        default=Recruitment.ApplyMethod.GOATZA
    )
    external_apply_url = serializers.URLField(
        required=False,
        allow_blank=True
    )

    # venue
    venue_name = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=255
    )
    venue_link = serializers.URLField(
        required=False,
        allow_blank=True
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
    age_categories = (
        RecruitmentAgeCategoryInputSerializer(
            many=True,
            required=False
        )
    )
    contacts = (
        RecruitmentContactInputSerializer(
            many=True,
            required=False
        )
    )
    benefits = (
        RecruitmentBenefitInputSerializer(
            many=True,
            required=False
        )
    )
    requirements = (
        RecruitmentRequirementInputSerializer(
            many=True,
            required=False
        )
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
        is_paid = attrs.get("is_paid")
        fee_amount = attrs.get("fee_amount")
        event_date = attrs.get("event_date")
        application_deadline = attrs.get("application_deadline")
        positions = attrs.get("positions", [])

        # AGE CATEGORY VALIDATION
        age_categories = attrs.get("age_categories", [])
        titles = [
            a["title"].lower()
            for a in age_categories
        ]
        if len(titles) != len(set(titles)):
            raise serializers.ValidationError(
                "Duplicate age categories."
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
        

        # apply method
        apply_method = attrs.get("apply_method")
        external_apply_url = attrs.get(
            "external_apply_url"
        )
        if (
            apply_method
            == Recruitment.ApplyMethod.EXTERNAL
            and not external_apply_url
        ):
            raise serializers.ValidationError(
                "external_apply_url required."
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
        

        # CONTACTS VALIDATION
        contacts = attrs.get("contacts", [])
        if (
            apply_method
            == Recruitment.ApplyMethod.CONTACT
            and not contacts
        ):
            raise serializers.ValidationError(
                "At least one contact required."
            )

        return attrs
