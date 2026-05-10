from django.db import transaction
from recruitments.models import (
    Recruitment,
    RecruitmentPosition,
    RecruitmentQuestion,
    RecruitmentQuestionOption,
    RecruitmentMedia,
)
from sports.models import Sport


class RecruitmentService:

    @staticmethod
    @transaction.atomic
    def create_recruitment(actor, validated_data):
        positions_data = validated_data.pop("positions", [])
        questions_data = validated_data.pop("questions", [])
        media_data = validated_data.pop("media", [])
        location_data = validated_data.pop("location", None)

        sport_id = validated_data.pop("sport_id")

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
            status=Recruitment.Status.ACTIVE, # handle this dynamically
            location=location,
            location_name=location_name,
            city=city,
            country_code=country_code,
            latitude=latitude,
            longitude=longitude,
            **validated_data
        )

        # POSITIONS
        position_objs = []
        for position_data in positions_data:
            position_objs.append(
                RecruitmentPosition(
                    recruitment=recruitment,
                    position_id=position_data["position_id"],
                    is_primary=position_data.get(
                        "is_primary",
                        False
                    )
                )
            )

        if position_objs:
            RecruitmentPosition.objects.bulk_create(position_objs)

        # QUESTIONS
        question_objs = []

        for idx, question_data in enumerate(questions_data):

            options_data = question_data.pop("options", [])

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

            option_objs = []

            for option_idx, option_data in enumerate(options_data):

                option_objs.append(
                    RecruitmentQuestionOption(
                        question=question,
                        value=option_data["value"],
                        display_order=option_idx
                    )
                )

            if option_objs:
                RecruitmentQuestionOption.objects.bulk_create(
                    option_objs
                )

        # MEDIA
        media_objs = []

        for idx, media in enumerate(media_data):

            media_objs.append(
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
            )

        if media_objs:
            RecruitmentMedia.objects.bulk_create(media_objs)

        return recruitment