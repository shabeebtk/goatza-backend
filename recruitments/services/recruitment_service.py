from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from recruitments.models import (
    Recruitment,
    RecruitmentPosition,
    RecruitmentQuestion,
    RecruitmentQuestionOption,
    RecruitmentMedia,
    RecruitmentAgeCategory,
    RecruitmentRequirement,
    RecruitmentBenefit,
    RecruitmentContact
)
from sports.models import Sport



class RecruitmentService:

    # Status state machine — the single source of truth on the server.
    # cancelled is terminal (no transitions out). Anything not listed here
    # (incl. same-status no-ops) is rejected as an invalid transition.
    ALLOWED_TRANSITIONS = {
        Recruitment.Status.DRAFT: {
            Recruitment.Status.ACTIVE,
            Recruitment.Status.CANCELLED,
        },
        Recruitment.Status.ACTIVE: {
            Recruitment.Status.CLOSED,
            Recruitment.Status.CANCELLED,
        },
        Recruitment.Status.CLOSED: {
            Recruitment.Status.ACTIVE,
        },
        Recruitment.Status.CANCELLED: set(),
    }

    @staticmethod
    @transaction.atomic
    def create_recruitment(actor, validated_data):

        positions_data = validated_data.pop("positions", [])
        questions_data = validated_data.pop("questions", [])
        media_data = validated_data.pop("media", [])

        age_categories_data = validated_data.pop("age_categories", [])
        contacts_data = validated_data.pop("contacts", [])
        benefits_data = validated_data.pop("benefits", [])
        requirements_data = validated_data.pop("requirements", [])

        location_data = validated_data.pop("location", None)

        sport_id = validated_data.pop("sport_id")

        # support direct publish or draft
        status = validated_data.pop(
            "status",
            Recruitment.Status.ACTIVE # change to draft once implemeted status
        )

        sport = Sport.objects.get(id=sport_id)

        # LOCATION
        location = None
        location_name = ""
        city = ""
        country_code = ""
        latitude = None
        longitude = None

        if location_data:
            location_name = location_data.get("name", "")
            city = location_data.get("city", "")
            country_code = location_data.get("country_code", "")
            latitude = location_data.get("latitude")
            longitude = location_data.get("longitude")

        # CREATE RECRUITMENT
        recruitment = Recruitment.objects.create(
            organization=actor.organization,
            created_by_member=getattr(
                actor,
                "organization_member",
                None
            ),
            sport=sport,
            status=status,

            location=location,
            location_name=location_name,
            city=city,
            country_code=country_code,
            latitude=latitude,
            longitude=longitude,

            **validated_data
        )

        # NESTED CHILDREN (shared with update via _sync_* helpers)
        RecruitmentService._sync_positions(recruitment, positions_data)
        RecruitmentService._sync_questions(recruitment, questions_data)
        RecruitmentService._sync_media(recruitment, media_data)
        RecruitmentService._sync_age_categories(
            recruitment, age_categories_data
        )
        RecruitmentService._sync_contacts(recruitment, contacts_data)
        RecruitmentService._sync_benefits(recruitment, benefits_data)
        RecruitmentService._sync_requirements(
            recruitment, requirements_data
        )

        return recruitment

    @staticmethod
    @transaction.atomic
    def update_recruitment(actor, recruitment, validated_data):

        positions_data = validated_data.pop("positions", [])
        questions_data = validated_data.pop("questions", [])
        media_data = validated_data.pop("media", [])

        age_categories_data = validated_data.pop("age_categories", [])
        contacts_data = validated_data.pop("contacts", [])
        benefits_data = validated_data.pop("benefits", [])
        requirements_data = validated_data.pop("requirements", [])

        location_data = validated_data.pop("location", None)

        sport_id = validated_data.pop("sport_id")

        # status is NOT changed here — transitions are a separate task.
        # The serializer has no status field, but pop defensively just in case.
        validated_data.pop("status", None)

        sport = Sport.objects.get(id=sport_id)

        # LOCATION — handled the same way create does
        location_name = ""
        city = ""
        country_code = ""
        latitude = None
        longitude = None

        if location_data:
            location_name = location_data.get("name", "")
            city = location_data.get("city", "")
            country_code = location_data.get("country_code", "")
            latitude = location_data.get("latitude")
            longitude = location_data.get("longitude")

        # SCALAR FIELDS
        recruitment.sport = sport
        recruitment.location_name = location_name
        recruitment.city = city
        recruitment.country_code = country_code
        recruitment.latitude = latitude
        recruitment.longitude = longitude

        for field, value in validated_data.items():
            setattr(recruitment, field, value)

        # Keep the paid/fee invariant (recruitment_valid_fee CheckConstraint):
        # a recruitment switched back to free must not keep a stale fee amount
        # from a previous paid state (the payload omits fee_amount when free).
        if not recruitment.is_paid:
            recruitment.fee_amount = None

        recruitment.save()

        # NESTED CHILDREN — replace-on-update (shared helpers)
        RecruitmentService._sync_positions(recruitment, positions_data)
        RecruitmentService._sync_questions(recruitment, questions_data)
        RecruitmentService._sync_media(recruitment, media_data)
        RecruitmentService._sync_age_categories(
            recruitment, age_categories_data
        )
        RecruitmentService._sync_contacts(recruitment, contacts_data)
        RecruitmentService._sync_benefits(recruitment, benefits_data)
        RecruitmentService._sync_requirements(
            recruitment, requirements_data
        )

        return recruitment

    @staticmethod
    @transaction.atomic
    def change_status(actor, recruitment, new_status):

        current_status = recruitment.status

        # same-status no-op → invalid
        if new_status == current_status:
            raise ValidationError(
                f"Recruitment is already {current_status}."
            )

        # enforce the state machine (cancelled is terminal)
        allowed = RecruitmentService.ALLOWED_TRANSITIONS.get(
            current_status,
            set()
        )
        if new_status not in allowed:
            raise ValidationError(
                f"Cannot change status from "
                f"{current_status} to {new_status}."
            )

        recruitment.status = new_status
        update_fields = ["status"]

        # publish side-effect: stamp published_at the first time it goes
        # active (draft→active or closed→active). Never overwrite an
        # existing published_at on reopen.
        if (
            new_status == Recruitment.Status.ACTIVE
            and recruitment.published_at is None
        ):
            recruitment.published_at = timezone.now()
            update_fields.append("published_at")

        recruitment.save(update_fields=update_fields)

        return recruitment

    # -----------------------------------------------------------------
    # NESTED CHILD HELPERS
    # Each helper deletes existing children for the recruitment and
    # bulk_creates from the incoming payload. On create the delete is a
    # no-op (no rows yet); on update it gives clean replace-on-update.
    # Shared by both create_recruitment and update_recruitment so the
    # child-building logic can never drift between the two paths.
    # -----------------------------------------------------------------

    @staticmethod
    def _sync_positions(recruitment, positions_data):
        recruitment.positions.all().delete()

        position_objs = [
            RecruitmentPosition(
                recruitment=recruitment,
                position_id=position["position_id"],
                is_primary=position.get(
                    "is_primary",
                    False
                )
            )
            for position in positions_data
        ]

        if position_objs:
            RecruitmentPosition.objects.bulk_create(
                position_objs
            )

    @staticmethod
    def _sync_questions(recruitment, questions_data):
        recruitment.questions.all().delete()

        for idx, question_data in enumerate(questions_data):

            options_data = question_data.pop(
                "options",
                []
            )

            question = RecruitmentQuestion.objects.create(
                recruitment=recruitment,
                question=question_data["question"],
                field_type=question_data["field_type"],
                is_required=question_data.get(
                    "is_required",
                    False
                ),
                placeholder=question_data.get(
                    "placeholder",
                    ""
                ),
                help_text=question_data.get(
                    "help_text",
                    ""
                ),
                display_order=idx
            )

            option_objs = [
                RecruitmentQuestionOption(
                    question=question,
                    value=option["value"],
                    display_order=option_idx
                )
                for option_idx, option
                in enumerate(options_data)
            ]

            if option_objs:
                RecruitmentQuestionOption.objects.bulk_create(
                    option_objs
                )

    @staticmethod
    def _sync_media(recruitment, media_data):
        recruitment.media.all().delete()

        media_objs = [
            RecruitmentMedia(
                recruitment=recruitment,
                file_url=media["file_url"],
                public_id=media["public_id"],
                media_type=media["media_type"],
                thumbnail_url=media.get(
                    "thumbnail_url",
                    ""
                ),
                duration=media.get("duration"),
                order=media.get("order", idx)
            )
            for idx, media in enumerate(media_data)
        ]

        if media_objs:
            RecruitmentMedia.objects.bulk_create(
                media_objs
            )

    @staticmethod
    def _sync_age_categories(recruitment, age_categories_data):
        recruitment.age_categories.all().delete()

        age_category_objs = [
            RecruitmentAgeCategory(
                recruitment=recruitment,
                title=age["title"],
                min_birth_year=age[
                    "min_birth_year"
                ],
                max_birth_year=age[
                    "max_birth_year"
                ],
                reporting_time=age.get(
                    "reporting_time"
                ),
                display_order=age.get(
                    "display_order",
                    idx
                )
            )
            for idx, age
            in enumerate(age_categories_data)
        ]

        if age_category_objs:
            RecruitmentAgeCategory.objects.bulk_create(
                age_category_objs
            )

    @staticmethod
    def _sync_contacts(recruitment, contacts_data):
        recruitment.contacts.all().delete()

        contact_objs = [
            RecruitmentContact(
                recruitment=recruitment,
                name=contact.get("name", ""),
                contact_type=contact[
                    "contact_type"
                ],
                value=contact["value"],
            )
            for contact in contacts_data
        ]

        if contact_objs:
            RecruitmentContact.objects.bulk_create(
                contact_objs
            )

    @staticmethod
    def _sync_benefits(recruitment, benefits_data):
        recruitment.benefits.all().delete()

        benefit_objs = [
            RecruitmentBenefit(
                recruitment=recruitment,
                title=benefit["title"],
                icon_name=benefit.get(
                    "icon_name",
                    ""
                ),
                display_order=benefit.get(
                    "display_order",
                    idx
                )
            )
            for idx, benefit
            in enumerate(benefits_data)
        ]

        if benefit_objs:
            RecruitmentBenefit.objects.bulk_create(
                benefit_objs
            )

    @staticmethod
    def _sync_requirements(recruitment, requirements_data):
        recruitment.requirements.all().delete()

        requirement_objs = [
            RecruitmentRequirement(
                recruitment=recruitment,
                title=requirement["title"],
                is_mandatory=requirement.get(
                    "is_mandatory",
                    True
                ),
                display_order=requirement.get(
                    "display_order",
                    idx
                )
            )
            for idx, requirement
            in enumerate(requirements_data)
        ]

        if requirement_objs:
            RecruitmentRequirement.objects.bulk_create(
                requirement_objs
            )
    


