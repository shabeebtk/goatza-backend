# recruitments/serializers/application_serializers.py
import re
from decimal import Decimal, InvalidOperation
from rest_framework import serializers
from accounts.models import User
from organization.models import Organization
from recruitments.models import (
    RecruitmentQuestion, RecruitmentApplication, Recruitment
)
from sports.serializers.sports_serializers import SportSerializer
# Reuse the exact same E.164-ish phone pattern the recruitment contact
# validation already uses, so the two can never drift.
from recruitments.serializers.recruitment_serializers import PHONE_RE


# Field types that are answered by picking option(s) rather than free text.
OPTION_FIELD_TYPES = {
    RecruitmentQuestion.FieldType.SELECT,
    RecruitmentQuestion.FieldType.RADIO,
    RecruitmentQuestion.FieldType.CHECKBOX,
}

# ...of those, the ones that accept exactly one option (checkbox accepts many).
SINGLE_OPTION_FIELD_TYPES = {
    RecruitmentQuestion.FieldType.SELECT,
    RecruitmentQuestion.FieldType.RADIO,
}


# ANSWER INPUT — shape only. All cross-field rules (belongs-to-recruitment,
# required, option ownership, number parsing) live in the parent's validate()
# where the recruitment + full answer set are available together.
class ApplicationAnswerInputSerializer(serializers.Serializer):
    question_id = serializers.UUIDField()
    answer_text = serializers.CharField(
        required=False,
        allow_blank=True
    )
    selected_option_ids = serializers.ListField(
        child=serializers.UUIDField(),
        required=False,
        default=list
    )


# APPLY SERIALIZER
class RecruitmentApplySerializer(serializers.Serializer):
    # Contact the applicant shares for THIS application. Prefilled from their
    # profile on the client, but user-editable — stored exactly as submitted.
    shared_name = serializers.CharField(max_length=255)
    shared_email = serializers.EmailField(
        required=False,
        allow_blank=True
    )
    shared_phone = serializers.CharField(max_length=15)

    answers = ApplicationAnswerInputSerializer(
        many=True,
        required=False,
        default=list
    )

    def validate_shared_name(self, value):
        value = (value or "").strip()
        if not value:
            raise serializers.ValidationError("Name is required.")
        return value

    def validate_shared_phone(self, value):
        value = (value or "").strip()
        if not value:
            raise serializers.ValidationError("Phone number is required.")

        # Strip common separators before matching, same as contact validation.
        normalized = re.sub(r"[\s\-().]", "", value)
        if not PHONE_RE.match(normalized):
            raise serializers.ValidationError("Enter a valid phone number.")

        # Store as submitted (the field's max_length=15 already caps the length).
        return value

    def validate(self, attrs):
        recruitment = self.context["recruitment"]
        answers = attrs.get("answers", [])

        # Questions (with options) are prefetched on the recruitment by the
        # selector, so these .all() calls hit the cache — no extra queries.
        questions = list(recruitment.questions.all())
        question_map = {str(q.id): q for q in questions}

        seen_question_ids = set()
        answered_question_ids = set()
        normalized_answers = []

        for answer in answers:
            question_id = str(answer["question_id"])

            # question must belong to THIS recruitment
            question = question_map.get(question_id)
            if not question:
                raise serializers.ValidationError(
                    f"Invalid question_id: {question_id}"
                )

            # no duplicate answers for the same question
            if question_id in seen_question_ids:
                raise serializers.ValidationError(
                    "Duplicate answers for the same question "
                    "are not allowed."
                )
            seen_question_ids.add(question_id)

            answer_text = (answer.get("answer_text") or "").strip()
            selected_option_ids = [
                str(option_id)
                for option_id in answer.get("selected_option_ids", [])
            ]

            field_type = question.field_type

            if field_type in OPTION_FIELD_TYPES:
                is_answered = self._validate_option_answer(
                    question,
                    selected_option_ids
                )
                # option-based answers never carry free text
                answer_text = ""
            else:
                is_answered = self._validate_text_answer(
                    question,
                    answer_text,
                    selected_option_ids
                )

            # Only keep answers that actually carry content — a present-but-empty
            # answer to an optional question is simply skipped (no noise rows).
            if is_answered:
                answered_question_ids.add(question_id)
                normalized_answers.append({
                    "question_id": question_id,
                    "answer_text": answer_text,
                    "selected_option_ids": selected_option_ids,
                })

        # every required question must be answered
        for question in questions:
            if (
                question.is_required
                and str(question.id) not in answered_question_ids
            ):
                raise serializers.ValidationError(
                    f"'{question.question}' is required."
                )

        attrs["answers"] = normalized_answers
        return attrs

    def _validate_option_answer(self, question, selected_option_ids):
        """
        Validate a select/radio/checkbox answer. Returns True if answered
        (>=1 option), False if empty (left blank). Raises on bad options or
        too many options for a single-choice field.
        """
        if not selected_option_ids:
            # left blank — required check handles it if the question is required
            return False

        # options within a single answer must be distinct
        if len(selected_option_ids) != len(set(selected_option_ids)):
            raise serializers.ValidationError(
                f"Duplicate options selected for '{question.question}'."
            )

        # every selected option must belong to THIS question
        valid_option_ids = {
            str(option.id)
            for option in question.options.all()
        }
        for option_id in selected_option_ids:
            if option_id not in valid_option_ids:
                raise serializers.ValidationError(
                    f"Invalid option for '{question.question}'."
                )

        # radio/select accept exactly one; checkbox accepts one or more
        if (
            question.field_type in SINGLE_OPTION_FIELD_TYPES
            and len(selected_option_ids) > 1
        ):
            raise serializers.ValidationError(
                f"Only one option can be selected for "
                f"'{question.question}'."
            )

        return True

    def _validate_text_answer(self, question, answer_text, selected_option_ids):
        """
        Validate a short_text/long_text/number answer. Returns True if answered
        (non-empty text), False if blank. Raises if options were sent for a text
        field or a number answer is not a finite number.
        """
        if selected_option_ids:
            raise serializers.ValidationError(
                f"Options are not allowed for '{question.question}'."
            )

        if not answer_text:
            return False

        if question.field_type == RecruitmentQuestion.FieldType.NUMBER:
            try:
                number = Decimal(answer_text)
                if not number.is_finite():
                    raise InvalidOperation
            except (InvalidOperation, ValueError):
                raise serializers.ValidationError(
                    f"'{question.question}' must be a valid number."
                )

        return True


# =========================================================
# ORG-SIDE READ SERIALIZERS (applicants listing)
# =========================================================

# APPLICANT MINI — the player behind an application. `avatar` maps to the
# profile photo; name/headline come off the one-to-one profile (loaded via
# select_related("applicant__profile"), so no N+1).
class ApplicantMiniSerializer(serializers.ModelSerializer):
    name = serializers.CharField(source="profile.name", read_only=True)
    avatar = serializers.URLField(
        source="profile.profile_photo",
        read_only=True
    )
    headline = serializers.CharField(
        source="profile.headline",
        read_only=True
    )

    class Meta:
        model = User
        fields = [
            "id",
            "username",
            "name",
            "avatar",
            "headline",
        ]


# LIST ITEM — one row in the org's applicants list.
class ApplicantListItemSerializer(serializers.ModelSerializer):
    applicant = ApplicantMiniSerializer(read_only=True)
    highlights_count = serializers.SerializerMethodField()

    class Meta:
        model = RecruitmentApplication
        fields = [
            "id",
            "status",
            "applied_at",
            "shared_name",
            "shared_email",
            "shared_phone",
            "applicant",
            "highlights_count",
        ]

    def get_highlights_count(self, obj):
        """
        Clips this viewer may watch, for the "▶ Highlights (n)" chip. Read from
        a per-page map the view builds in one query (see
        highlights.selectors.visible_highlight_counts_for) — never queried here,
        or the list would fan out one COUNT per row.

        None when the caller didn't supply the map (e.g. the detail endpoint):
        the client treats that as "unknown" and fetches on demand instead of
        rendering a wrong 0.
        """
        counts = self.context.get("highlight_counts")
        if counts is None:
            return None
        return counts.get(obj.applicant_id, 0)


# DETAIL — list-item fields + the answered custom questions. The stored answers
# are one row per question for text/number/single-choice and one row PER OPTION
# for checkbox; get_answers regroups them into a single object per question,
# ordered by the question's display_order.
class ApplicationDetailSerializer(ApplicantListItemSerializer):
    answers = serializers.SerializerMethodField()

    class Meta(ApplicantListItemSerializer.Meta):
        fields = ApplicantListItemSerializer.Meta.fields + ["answers"]

    def get_answers(self, obj):
        # obj.answers is prefetched (ordered by question display_order) with
        # question + selected_option select_related — this loop issues no queries.
        grouped = {}
        order = []

        for answer in obj.answers.all():
            question = answer.question
            question_id = str(question.id)

            if question_id not in grouped:
                grouped[question_id] = {
                    "question": question.question,
                    "field_type": question.field_type,
                    "answer_text": "",
                    "selected_options": [],
                    "_display_order": question.display_order,
                }
                order.append(question_id)

            entry = grouped[question_id]

            if answer.answer_text:
                entry["answer_text"] = answer.answer_text

            # selected_option is SET_NULL — skip an option deleted after applying.
            if answer.selected_option_id and answer.selected_option:
                entry["selected_options"].append(
                    answer.selected_option.value
                )

        result = [grouped[question_id] for question_id in order]
        result.sort(key=lambda entry: entry["_display_order"])

        for entry in result:
            entry.pop("_display_order")

        return result


# =========================================================
# PLAYER-SIDE READ SERIALIZERS (my applications)
# =========================================================

# ORG MINI — the recruiting org behind an application, as the player sees it.
# `logo` lives on the one-to-one org profile (loaded via
# select_related("recruitment__organization__profile"), so no N+1).
class MyApplicationOrgMiniSerializer(serializers.ModelSerializer):
    logo = serializers.SerializerMethodField()

    class Meta:
        model = Organization
        fields = [
            "id",
            "name",
            "username",
            "logo",
            "is_verified",
        ]

    def get_logo(self, obj):
        profile = getattr(obj, "profile", None)
        if profile and profile.logo:
            return profile.logo
        return ""


# RECRUITMENT SUMMARY — the slice of the recruitment the player's applications
# list needs, with the org + sport nested.
class MyApplicationRecruitmentSerializer(serializers.ModelSerializer):
    organization = MyApplicationOrgMiniSerializer(read_only=True)
    sport = SportSerializer(read_only=True)

    class Meta:
        model = Recruitment
        fields = [
            "id",
            "title",
            "recruitment_type",
            "status",
            "city",
            "event_date",
            "application_deadline",
            "organization",
            "sport",
        ]


# LIST ITEM — one row in the authenticated player's own applications list.
class MyApplicationListSerializer(serializers.ModelSerializer):
    recruitment = MyApplicationRecruitmentSerializer(read_only=True)

    class Meta:
        model = RecruitmentApplication
        fields = [
            "id",
            "status",
            "applied_at",
            "updated_at",
            "recruitment",
        ]


# =========================================================
# ORG STATUS-CHANGE REQUEST SERIALIZERS
# =========================================================

# Bulk multi-select targets (Invited is single-only, per product decision #3).
BULK_STATUS_TARGETS = [
    RecruitmentApplication.Status.REVIEWING,
    RecruitmentApplication.Status.SHORTLISTED,
    RecruitmentApplication.Status.SELECTED,
    RecruitmentApplication.Status.REJECTED,
]

# Single-change (drawer) targets — free transitions. `invited` is reserved for
# the future personal-invite feature and is NOT offered in the org UI/API.
SINGLE_STATUS_TARGETS = [
    RecruitmentApplication.Status.REVIEWING,
    RecruitmentApplication.Status.SHORTLISTED,
    RecruitmentApplication.Status.SELECTED,
    RecruitmentApplication.Status.REJECTED,
]


class BulkApplicationStatusSerializer(serializers.Serializer):
    application_ids = serializers.ListField(
        child=serializers.UUIDField(),
        allow_empty=False,
        max_length=100,
    )
    status = serializers.ChoiceField(choices=BULK_STATUS_TARGETS)
    note = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
        max_length=1000,
    )


class SingleApplicationStatusSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=SINGLE_STATUS_TARGETS)
    note = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
        max_length=1000,
    )
