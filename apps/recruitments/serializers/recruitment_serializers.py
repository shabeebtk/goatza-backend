import re
from datetime import datetime, time
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.validators import validate_email
from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils import timezone
from rest_framework import serializers
from apps.recruitments.models import (
    Recruitment,
    RecruitmentQuestion,
    RecruitmentContact,
)
from apps.sports.models import SportPosition, Sport
from shared.models import Location

# E.164-ish phone: optional leading +, then 7–15 digits (separators stripped).
PHONE_RE = re.compile(r"^\+?\d{7,15}$")

# POSITION INPUT
class RecruitmentPositionInputSerializer(serializers.Serializer):
    position_id = serializers.UUIDField()
    is_primary = serializers.BooleanField(default=False)


# QUESTION OPTION INPUT
class RecruitmentQuestionOptionInputSerializer(serializers.Serializer):
    value = serializers.CharField(max_length=255)


# AGE CATEGORY INPUT
# `id` is optional and only meaningful on update — the service diff-syncs on
# it so an edit updates a group in place instead of recreating it (which would
# drop the group every applicant applied under).
# Both birth years are optional: leaving one out makes the group open-ended
# ("born 2010 or later"). A group with NEITHER is meaningless — "open to all
# ages" is expressed by sending an empty age_categories list.
class RecruitmentAgeCategoryInputSerializer(
    serializers.Serializer
):

    id = serializers.UUIDField(required=False)
    title = serializers.CharField(max_length=50)
    min_birth_year = serializers.IntegerField(
        min_value=1950,
        required=False,
        allow_null=True
    )
    max_birth_year = serializers.IntegerField(
        min_value=1950,
        required=False,
        allow_null=True
    )
    reporting_time = serializers.TimeField(
        required=False,
        allow_null=True
    )
    display_order = serializers.IntegerField(default=0)

    def validate(self, attrs):

        min_birth_year = attrs.get("min_birth_year")
        max_birth_year = attrs.get("max_birth_year")

        # mirrors the age_category_birth_year_required DB constraint
        if (
            min_birth_year is None
            and max_birth_year is None
        ):
            raise serializers.ValidationError(
                "Enter a minimum or a maximum birth year."
            )

        if (
            min_birth_year is not None
            and max_birth_year is not None
            and min_birth_year > max_birth_year
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

    def validate(self, attrs):
        # bulk_create skips model.clean(), so this serializer is the only gate.
        contact_type = attrs.get("contact_type")
        value = (attrs.get("value") or "").strip()

        if contact_type == RecruitmentContact.ContactType.PHONE:
            normalized = re.sub(r"[\s\-().]", "", value)
            if not PHONE_RE.match(normalized):
                raise serializers.ValidationError(
                    "Enter a valid phone number."
                )

        elif contact_type == RecruitmentContact.ContactType.EMAIL:
            try:
                validate_email(value)
            except DjangoValidationError:
                raise serializers.ValidationError(
                    "Enter a valid email address."
                )

        return attrs


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
    file_url = serializers.URLField(max_length=500)
    public_id = serializers.CharField(max_length=255)
    media_type = serializers.ChoiceField(
        choices=["image", "video"]
    )
    thumbnail_url = serializers.URLField(
        required=False,
        allow_blank=True,
        max_length=500
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


# ELIGIBILITY CRITERIA INPUT
# Free-text "who can attend" lines authored by the org. Displayed only — the
# platform never checks an applicant against them.
class RecruitmentEligibilityCriteriaInputSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=255)
    display_order = serializers.IntegerField(default=0)


# LOCATION INPUT
# The shared place payload (docs/PLACES_MIGRATION.md 5.4). ``provider`` +
# ``external_id`` are the Location's identity, so they have to survive the
# serializer — a strict Serializer drops anything not declared here, which is
# exactly how a recruitment would end up creating a duplicate Location row.
class RecruitmentLocationInputSerializer(serializers.Serializer):
    provider = serializers.ChoiceField(
        choices=Location.Provider.choices,
        required=False,
        default=Location.Provider.GOOGLE,
    )
    external_id = serializers.CharField(
        max_length=255, required=False, allow_blank=True
    )
    name = serializers.CharField(max_length=255)
    type = serializers.ChoiceField(
        choices=Location.Type.choices,
        required=False,
        default=Location.Type.PLACE,
    )
    # city / state / country / country_code are denormalized display strings —
    # the model allows them blank and the service reads them with a default, so
    # don't hard-require them. A venue result outside a named locality may carry
    # no city or country at all.
    city = serializers.CharField(max_length=100, required=False, allow_blank=True)
    state = serializers.CharField(max_length=100, required=False, allow_blank=True)
    country = serializers.CharField(
        max_length=100, required=False, allow_blank=True
    )
    country_code = serializers.CharField(
        max_length=5, required=False, allow_blank=True
    )
    # Nullable now: a place is identified by its id, and its coordinates are a
    # cache that can be absent or expired.
    latitude = serializers.FloatField(required=False, allow_null=True)
    longitude = serializers.FloatField(required=False, allow_null=True)


# TRIAL SESSION INPUT
# One date an open trial runs on. `id` is optional and only meaningful on
# update — the service diff-syncs on it so an edit moves a date in place
# instead of recreating it (which would drop the date every applicant picked).
# The venue block is the same place payload the recruitment itself takes;
# leaving it out means "inherit the recruitment's venue".
class TrialSessionInputSerializer(serializers.Serializer):

    id = serializers.UUIDField(required=False)
    title = serializers.CharField(
        max_length=120, required=False, allow_blank=True
    )
    date = serializers.DateField()
    start_time = serializers.TimeField(required=False, allow_null=True)
    end_time = serializers.TimeField(required=False, allow_null=True)
    venue_name = serializers.CharField(
        max_length=255, required=False, allow_blank=True
    )
    venue_link = serializers.URLField(
        max_length=500, required=False, allow_blank=True
    )
    location = RecruitmentLocationInputSerializer(required=False)
    is_cancelled = serializers.BooleanField(default=False)
    display_order = serializers.IntegerField(default=0)

    def validate(self, attrs):
        start_time = attrs.get("start_time")
        end_time = attrs.get("end_time")

        if start_time and end_time and end_time <= start_time:
            raise serializers.ValidationError(
                "The end time must be after the start time."
            )

        return attrs


def session_starts_at(session):
    """
    A validated session payload row as an aware datetime, the same way
    ``TrialSession.starts_at`` reads a stored one: its start time, or 23:59
    when it carries none. Used here to check the deadline against the first
    date BEFORE the row exists.
    """
    return datetime.combine(
        session["date"],
        session.get("start_time") or time(23, 59),
        tzinfo=ZoneInfo(settings.RECRUITMENT_TIMEZONE),
    )


def first_session(sessions):
    """The non-cancelled session a trial starts on, or None."""
    live = [s for s in sessions if not s.get("is_cancelled")]
    if not live:
        return None
    return min(
        live,
        key=lambda s: (
            s["date"],
            s.get("start_time") or time(23, 59),
            s.get("display_order", 0),
        ),
    )


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
    # Only means anything with 2+ dates; validate() forces it back to
    # `all` below when there are fewer, rather than erroring.
    session_mode = serializers.ChoiceField(
        choices=Recruitment.SessionMode.choices,
        default=Recruitment.SessionMode.ALL
    )
    # An open-trial setting. validate() forces it off on every other type
    # rather than erroring: it is a checkbox the wizard hides, so a stray
    # true is a stale draft, not something the org asked for and should be
    # argued with.
    auto_confirm = serializers.BooleanField(default=False)
    # Draft vs publish on create only. Other transitions (close/cancel/reopen)
    # go through the /status endpoint state machine, so update ignores this.
    status = serializers.ChoiceField(
        choices=[
            Recruitment.Status.DRAFT,
            Recruitment.Status.ACTIVE,
        ],
        default=Recruitment.Status.ACTIVE
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
    sessions = TrialSessionInputSerializer(
        many=True,
        required=False
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
    eligibility_criteria = (
        RecruitmentEligibilityCriteriaInputSerializer(
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
        # An empty list is valid and means "open to any position".
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
        recruitment_type = attrs.get("recruitment_type")

        # TRIAL DATES
        sessions = attrs.get("sessions", [])
        live_sessions = [
            session for session in sessions
            if not session.get("is_cancelled")
        ]

        if recruitment_type == Recruitment.Type.OPEN_TRIAL:
            if not sessions:
                raise serializers.ValidationError(
                    "Add at least one trial date."
                )
            if not live_sessions:
                raise serializers.ValidationError(
                    "Every trial date is cancelled. Add a date, or "
                    "close the trial instead."
                )
        elif sessions:
            # player_looking (and the retired legacy types) have no trial
            # day at all, so a date sent for one is a client bug, not
            # something to silently drop.
            raise serializers.ValidationError(
                "Only an open trial can have trial dates."
            )

        # No two live dates may sit at the same date AND time — that is a
        # duplicated row, not a second round.
        slots = [
            (session["date"], session.get("start_time"))
            for session in live_sessions
        ]
        if len(slots) != len(set(slots)):
            raise serializers.ValidationError(
                "Two trial dates are the same. Remove the duplicate."
            )

        # session_mode is a question only a multi-date trial can answer.
        # With one date there is nothing to choose between, so force `all`
        # rather than reject a stale value the wizard left behind.
        if len(live_sessions) < 2:
            attrs["session_mode"] = Recruitment.SessionMode.ALL

        # AN OPEN-TRIAL-ONLY SETTING. Silently forced off elsewhere — see
        # the field declaration.
        if recruitment_type != Recruitment.Type.OPEN_TRIAL:
            attrs["auto_confirm"] = False

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
        if apply_method == Recruitment.ApplyMethod.EXTERNAL:
            if not external_apply_url:
                raise serializers.ValidationError(
                    "external_apply_url required."
                )
        else:
            # non-external methods must not carry a stray apply URL
            attrs["external_apply_url"] = ""

        # DATE VALIDATION
        now = timezone.now()

        # On UPDATE, an unchanged (already-stored) past deadline is allowed —
        # only enforce "not in the past" when the value actually changes.
        instance = self.context.get("recruitment")
        deadline_unchanged = (
            instance is not None
            and instance.application_deadline == application_deadline
        )

        if (
            application_deadline
            and application_deadline < now
            and not deadline_unchanged
        ):
            raise serializers.ValidationError(
                "Application deadline cannot be in the past"
            )

        # deadline <= event_date always (also the valid_application_deadline
        # DB CheckConstraint). event_date is DERIVED from the first
        # non-cancelled session, so when dates are sent it is that session
        # the deadline is measured against — an edit that moves day one
        # earlier can otherwise put an already-stored deadline in breach
        # and blow up on the constraint with nothing readable to show.
        # The deadline is never silently clamped: the org set it on purpose.
        opening_session = first_session(sessions)
        if application_deadline and opening_session:
            starts_at = session_starts_at(opening_session)
            if application_deadline > starts_at:
                local = starts_at.astimezone(
                    ZoneInfo(settings.RECRUITMENT_TIMEZONE)
                )
                raise serializers.ValidationError(
                    "The application deadline is after the first trial "
                    "date. Move the deadline to on or before "
                    f"{local.strftime('%d %b %Y')}."
                )
        elif (
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


# UPDATE RECRUITMENT SERIALIZER
# Subclasses the create serializer so every field + cross-field
# validation rule is shared and can never drift between create/update.
# Update accepts the same full-object shape the create endpoint accepts,
# except `status` — status transitions live in the /status endpoint.
# The view passes the existing instance via context["recruitment"] so the
# deadline past-date rule can be skipped when the value is unchanged.
class RecruitmentUpdateSerializer(RecruitmentCreateSerializer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields.pop("status", None)


# CHANGE STATUS SERIALIZER
# Only validates that `status` is one of the known choices. The allowed
# transition rules (the state machine) live in the service layer.
class ChangeRecruitmentStatusSerializer(serializers.Serializer):
    status = serializers.ChoiceField(
        choices=Recruitment.Status.choices
    )
