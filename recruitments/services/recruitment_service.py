from django.db import transaction
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

        # POSITIONS
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

        # QUESTIONS
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

        # MEDIA
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

        # AGE CATEGORIES
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

        # CONTACTS
        contact_objs = [
            RecruitmentContact(
                recruitment=recruitment,
                name=contact.get("name", ""),
                contact_type=contact[
                    "contact_type"
                ],
                value=contact["value"],
            )
            for idx, contact
            in enumerate(contacts_data)
        ]

        if contact_objs:
            RecruitmentContact.objects.bulk_create(
                contact_objs
            )

        # BENEFITS
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

        # REQUIREMENTS
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

        return recruitment
    


