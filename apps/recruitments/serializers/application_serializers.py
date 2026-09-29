# recruitments/serializers/application_serializers.py
import re
from decimal import Decimal, InvalidOperation
from rest_framework import serializers
from apps.accounts.models import User
from apps.organization.models import Organization
from apps.recruitments.models import (
    RecruitmentQuestion, RecruitmentApplication, Recruitment,
    RecruitmentApplicationStatusHistory,
)
from apps.sports.serializers.sports_serializers import SportSerializer
# Reuse the exact same E.164-ish phone pattern the recruitment contact
# validation already uses, so the two can never drift.
from apps.recruitments.serializers.recruitment_serializers import PHONE_RE
from apps.recruitments.legacy_status import LEGACY_STATUSES
from apps.recruitments.serializers.recruitment_list_serializers import (
    ApplicationAgeCategorySerializer,
    application_session_payload,
)
from apps.recruitments.feedback_window import (
    can_give_feedback,
    prompt_window_open,
)


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

    # The age group the applicant is applying under. Optional at the API level
    # (older clients, and eligibility is never enforced by the platform) — the
    # client requires a choice in the UI when the recruitment has groups.
    age_category = serializers.UUIDField(
        required=False,
        allow_null=True
    )

    # The trial date they are attending. Required, belongs-to-this-trial,
    # not-cancelled and not-past are all decided by
    # ApplicationService._resolve_session, under the recruitment row lock
    # — a date that is open when the form renders can be cancelled before
    # it is submitted, so the shape is all this field can honestly check.
    session = serializers.UUIDField(
        required=False,
        allow_null=True
    )

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

        # AGE CATEGORY — same belongs-to-THIS-recruitment rule the answers get
        # below. A group id from another recruitment is rejected outright; it is
        # never silently dropped, or the org would filter on a group the player
        # never picked. (Age categories are prefetched by the apply selector.)
        age_category_id = attrs.get("age_category")
        if age_category_id is not None:
            valid_category_ids = {
                str(category.id)
                for category in recruitment.age_categories.all()
            }
            if str(age_category_id) not in valid_category_ids:
                raise serializers.ValidationError(
                    "Invalid age group for this recruitment."
                )

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
    # null when the recruitment had no age groups, or the applicant applied
    # before groups existed / the group was deleted on a later edit.
    age_category = ApplicationAgeCategorySerializer(read_only=True)
    # Which date they said they would come to, on a choose_one trial.
    # Null on every other posting — there is nothing to pick.
    session = serializers.SerializerMethodField()
    # The trial fee as the gate recorded it. INFORMATION, never a gate:
    # nothing reads it to decide whether somebody may be confirmed or
    # selected.
    fee_marked_by = serializers.SerializerMethodField()

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
            "age_category",
            "session",
            "fee_paid",
            "fee_paid_at",
            "fee_marked_by",
            # THE PLAYER'S OWN ACCOUNT, enough for a chip on the row. A HINT:
            # it is what the player says, never what the org decided —
            # `status` above is the only thing that carries a decision. The
            # rating and the written note are DETAIL-only, so a list the org
            # scans does not show a number next to a face.
            "attended_self_reported",
            "outcome_self_reported",
            "feedback_at",
            # Birth year outside the chosen group when they applied. A column,
            # computed server-side by ApplicationService.apply — no query.
            "age_mismatch_at_apply",
        ]

    def get_session(self, obj):
        return application_session_payload(obj)

    def get_fee_marked_by(self, obj):
        """
        The member who marked it, for the tooltip. SET_NULL, so an
        member who has since left reads as null rather than taking the
        fee record with them. select_related by the list selector.
        """
        member = obj.fee_marked_by
        if member is None:
            return None

        profile = getattr(member.user, "profile", None)
        return {
            "id": str(member.id),
            "name": getattr(profile, "name", "") or member.user.username or "",
        }

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


# STATUS HISTORY ENTRY — one move in an application's pipeline, for the
# drawer's timeline. `changed_by` is the org member who made it, or null for
# the applicant's own moves and for system writes (the v3 status migration).
# The member → user → profile chain is select_related on the prefetch in
# ApplicationSelector.get_application_detail, so this issues no queries.
class ApplicationStatusHistorySerializer(serializers.ModelSerializer):
    changed_by = serializers.SerializerMethodField()

    class Meta:
        model = RecruitmentApplicationStatusHistory
        fields = [
            "id",
            "from_status",
            "to_status",
            "note",
            "created_at",
            "changed_by",
        ]

    def get_changed_by(self, obj):
        member = obj.changed_by
        if member is None:
            return None

        profile = getattr(member.user, "profile", None)
        return {
            "id": str(member.id),
            "name": getattr(profile, "name", "") or member.user.username or "",
        }


# DETAIL — list-item fields + the answered custom questions. The stored answers
# are one row per question for text/number/single-choice and one row PER OPTION
# for checkbox; get_answers regroups them into a single object per question,
# ordered by the question's display_order.
class ApplicationDetailSerializer(ApplicantListItemSerializer):
    answers = serializers.SerializerMethodField()
    applicant_birth_year = serializers.SerializerMethodField()
    # Newest first — the prefetch in get_application_detail sets the order.
    status_history = ApplicationStatusHistorySerializer(
        many=True, read_only=True
    )

    class Meta(ApplicantListItemSerializer.Meta):
        fields = ApplicantListItemSerializer.Meta.fields + [
            "answers",
            "applicant_birth_year",
            "status_history",
            # THE OWNING ORG ONLY. This serializer is already behind the
            # ownership gate on the detail view, and that gate is what keeps
            # a rating private — there is no `is_public` column deciding it.
            # If ratings ever go public, it is this line that changes.
            "trial_rating",
            "trial_feedback",
        ]

    def get_applicant_birth_year(self, obj):
        """
        The LIVE profile birth year, next to the frozen age_mismatch_at_apply,
        so the drawer can tell "mismatched at apply" from "since corrected".
        applicant__profile is select_related by the detail selector.
        """
        profile = getattr(obj.applicant, "profile", None)
        birthdate = getattr(profile, "birthdate", None)
        return birthdate.year if birthdate else None

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
    # My applications keeps ended trials on purpose (it is the player's own
    # history); this flag is how the row says the day has passed. A property
    # over event_date, so it costs nothing on the select_related row.
    is_trial_over = serializers.BooleanField(read_only=True)
    # The other half of the window: when applications stopped, which on an
    # "attend every date" trial is the FIRST date, not the last.
    trial_end_date = serializers.DateTimeField(read_only=True)
    applications_close_at = serializers.DateTimeField(read_only=True)
    session_mode = serializers.CharField(read_only=True)

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
            "is_trial_over",
            "trial_end_date",
            "applications_close_at",
            "session_mode",
            # The player's row shows a fee line only when there is a
            # fee to show.
            "is_paid",
            "fee_amount",
            "fee_currency",
            "organization",
            "sport",
        ]


# LIST ITEM — one row in the authenticated player's own applications list.
class MyApplicationListSerializer(serializers.ModelSerializer):
    recruitment = MyApplicationRecruitmentSerializer(read_only=True)
    age_category = ApplicationAgeCategorySerializer(read_only=True)
    # The date they picked, with its venue resolved — the one thing a
    # player on a city tour needs the row to say. Null unless choose_one.
    session = serializers.SerializerMethodField()
    # Read-only for the player: whether the org has marked their fee
    # collected. Nothing here is theirs to change.
    fee_paid = serializers.BooleanField(read_only=True)
    # THEIR OWN ANSWERS, handed back so the client shows "You rated this 4★"
    # instead of asking again. Never another player's — this serializer is
    # only ever built from the caller's own rows.
    can_give_feedback = serializers.SerializerMethodField()
    feedback_window_open = serializers.SerializerMethodField()

    class Meta:
        model = RecruitmentApplication
        fields = [
            "id",
            "status",
            "applied_at",
            "updated_at",
            "recruitment",
            "age_category",
            "session",
            "fee_paid",
            "attended_self_reported",
            "outcome_self_reported",
            "trial_rating",
            "trial_feedback",
            "feedback_at",
            # SHOULD WE ASK. Computed server-side so the client never
            # re-derives "trial over, and my status is one of these three"
            # from dates and statuses and gets it subtly wrong — which is
            # how a prompt appears that then 400s. Show the prompt when
            # BOTH are true; see feedback_window.py.
            "can_give_feedback",
            "feedback_window_open",
        ]

    def get_session(self, obj):
        return application_session_payload(obj)

    def get_can_give_feedback(self, obj):
        return can_give_feedback(obj, obj.recruitment)

    def get_feedback_window_open(self, obj):
        return prompt_window_open(obj.recruitment)


# =========================================================
# ORG STATUS-CHANGE REQUEST SERIALIZERS
# =========================================================

# Bulk multi-select targets. Mirrors ApplicationService.STATUS_CHANGE_TARGETS,
# in pipeline order: `invited` and `rejected` stay valid values on old rows but
# are no longer settable.
BULK_STATUS_TARGETS = [
    RecruitmentApplication.Status.REVIEWING,
    RecruitmentApplication.Status.SHORTLISTED,
    RecruitmentApplication.Status.TRIAL_CONFIRMED,
    RecruitmentApplication.Status.NOT_SHORTLISTED,
    RecruitmentApplication.Status.SELECTED,
    RecruitmentApplication.Status.NOT_SELECTED,
]

# Single-change (drawer) targets — free transitions, same set as bulk.
SINGLE_STATUS_TARGETS = list(BULK_STATUS_TARGETS)

# WHAT THE ENDPOINTS ACCEPT, which is deliberately wider than what they OFFER.
#
# Goatza is an installed PWA: old JavaScript lives on phones for days after a
# deploy and still sends `invited` or `rejected`. A ChoiceField that refuses
# them 400s before ApplicationService ever sees the request, and the mapping
# that exists precisely to rescue those calls never runs — so the accepted set
# includes them and the service translates
# (ApplicationService._map_legacy_target).
#
# The TARGETS lists above stay clean: they are what the client renders as
# buttons, and nothing should offer a retired status to somebody choosing one.
ACCEPTED_STATUS_VALUES = [*BULK_STATUS_TARGETS, *LEGACY_STATUSES]


# =========================================================
# TRIAL FEEDBACK REQUEST SERIALIZER (the player's own account)
# =========================================================

class TrialFeedbackSerializer(serializers.Serializer):
    """
    "How did the trial go?", as the PLAYER answers it.

    DID NOT ATTEND IS AN ACCEPTED ANSWER, not an error. Rating a trial you
    did not go to is meaningless and so is an outcome, so both are forced
    empty here rather than argued with — the request succeeds and the org
    learns something true.

    The rating is required of somebody who DID attend: a row that says "I
    came" and nothing else tells the org nothing they did not already have.
    The written note stays optional, always — most people will not write one,
    and demanding prose is how you get "good".
    """

    attended = serializers.BooleanField()
    outcome = serializers.ChoiceField(
        choices=RecruitmentApplication.SelfOutcome.choices,
        required=False,
        allow_blank=True,
    )
    rating = serializers.IntegerField(
        min_value=1, max_value=5, required=False, allow_null=True,
    )
    feedback = serializers.CharField(
        max_length=1000, required=False, allow_blank=True,
    )

    def validate_feedback(self, value):
        return (value or "").strip()

    def validate(self, attrs):
        if not attrs["attended"]:
            # NOT an error — see the class docstring. Anything the client
            # left in the form is dropped here so the columns cannot
            # disagree with the answer.
            attrs["outcome"] = ""
            attrs["rating"] = None
            return attrs

        if attrs.get("rating") is None:
            raise serializers.ValidationError(
                {"rating": "Rate the trial from 1 to 5."}
            )

        # REQUIRED of an attendee, because the three choices cover every
        # state a player can be in — including not knowing yet, which is
        # what `waiting` is for. A blank outcome on somebody who attended
        # would be a hole in the org's filter.
        if not attrs.get("outcome"):
            raise serializers.ValidationError(
                {"outcome": "Tell us how it went."}
            )

        return attrs


# =========================================================
# TRIAL FEE REQUEST SERIALIZERS
# =========================================================

class ApplicationFeeSerializer(serializers.Serializer):
    """One application's fee flag. `false` unmarks it."""
    fee_paid = serializers.BooleanField()


class MessageApplicantsSerializer(serializers.Serializer):
    """"Message these players." Capped at the same 100 the bulk status uses."""

    application_ids = serializers.ListField(
        child=serializers.UUIDField(),
        allow_empty=False,
        max_length=100,
    )
    body = serializers.CharField(max_length=1000)

    def validate_body(self, value):
        value = (value or "").strip()
        if not value:
            raise serializers.ValidationError("Write something to send.")
        return value


class BulkApplicationFeeSerializer(serializers.Serializer):
    """Same shape as the bulk status request, capped the same way."""
    application_ids = serializers.ListField(
        child=serializers.UUIDField(),
        allow_empty=False,
        max_length=100,
    )
    fee_paid = serializers.BooleanField()


class BulkApplicationStatusSerializer(serializers.Serializer):
    application_ids = serializers.ListField(
        child=serializers.UUIDField(),
        allow_empty=False,
        max_length=100,
    )
    status = serializers.ChoiceField(choices=ACCEPTED_STATUS_VALUES)
    note = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
        max_length=1000,
    )


class SingleApplicationStatusSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=ACCEPTED_STATUS_VALUES)
    note = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
        max_length=1000,
    )
