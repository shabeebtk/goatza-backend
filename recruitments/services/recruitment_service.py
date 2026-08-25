import logging
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
    RecruitmentContact,
    RecruitmentEligibilityCriteria
)
from sports.models import Sport
from services.storage.factory import get_storage_service
from services.storage.validators import (
    allowed_image_extensions,
    allowed_video_extensions,
    validate_media,
    validate_thumbnail,
)


logger = logging.getLogger(__name__)


class RecruitmentService:

    # Per-media-type upload whitelist (server-side, mirrors the frontend picker).
    # Chosen per provider: the R2 path serves the exact uploaded bytes, so it
    # cannot accept a .mov nothing will transcode. See
    # services/storage/validators.allowed_video_extensions.
    @staticmethod
    def _media_extensions(media_type):
        if media_type == "image":
            return allowed_image_extensions()
        if media_type == "video":
            return allowed_video_extensions()
        return set()

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
        eligibility_criteria_data = validated_data.pop(
            "eligibility_criteria", []
        )

        location_data = validated_data.pop("location", None)

        sport_id = validated_data.pop("sport_id")

        # honor the validated draft/active status (serializer defaults to active)
        status = validated_data.pop(
            "status",
            Recruitment.Status.ACTIVE
        )

        # stamp published_at only when created directly as active; drafts stay
        # null (change_status stamps it on first activation later).
        published_at = (
            timezone.now()
            if status == Recruitment.Status.ACTIVE
            else None
        )

        # verify every media asset belongs to this org before writing anything
        RecruitmentService._validate_media(actor, media_data)

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
            published_at=published_at,

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
        RecruitmentService._sync_eligibility_criteria(
            recruitment, eligibility_criteria_data
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
        eligibility_criteria_data = validated_data.pop(
            "eligibility_criteria", []
        )

        location_data = validated_data.pop("location", None)

        sport_id = validated_data.pop("sport_id")

        # status is NOT changed here — transitions are a separate task.
        # The serializer has no status field, but pop defensively just in case.
        validated_data.pop("status", None)

        # Applicant-safe edit rules — you can't change the game once people
        # have applied.
        if recruitment.applications_count > 0:
            if str(sport_id) != str(recruitment.sport_id):
                raise ValidationError(
                    "Sport cannot be changed after applications "
                    "have been received"
                )

            new_max = validated_data.get("max_applications")
            if (
                new_max is not None
                and new_max < recruitment.applications_count
            ):
                raise ValidationError(
                    f"Max applications cannot be below the current "
                    f"{recruitment.applications_count} application(s)."
                )

        # verify every media asset belongs to this org before writing anything
        RecruitmentService._validate_media(actor, media_data)

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
        # a recruitment switched back to free must not keep stale fee data from a
        # previous paid state (the payload omits these fields when free, so the
        # setattr loop above never clears them).
        if not recruitment.is_paid:
            recruitment.fee_amount = None
            recruitment.payment_note = ""
            recruitment.fee_currency = "INR"

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
        RecruitmentService._sync_eligibility_criteria(
            recruitment, eligibility_criteria_data
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
    # MEDIA VALIDATION + CLEANUP
    # -----------------------------------------------------------------

    # Map the raw validator errors to clear, per-item messages.
    _MEDIA_ERROR_DETAIL = {
        "Invalid media source": "file is not from an allowed media source",
        "Invalid public_id path": "file does not belong to this organization",
        "Public ID mismatch": "file URL does not match its file id",
    }

    @staticmethod
    def _media_error(idx, raw):
        detail = RecruitmentService._MEDIA_ERROR_DETAIL.get(raw)
        if detail is None:
            if raw.startswith("Invalid file type"):
                detail = f"unsupported {raw.split(':', 1)[-1].strip()} file type"
            else:
                detail = raw
        return ValidationError(f"media[{idx}]: {detail}")

    @staticmethod
    def _validate_media(actor, media_data):
        """
        Verify every media item is a stored asset owned by this org:
        source + extension whitelist + public_id path-ownership + URL↔public_id
        match. Thumbnails, when present, get the same checks plus a same-folder
        rule.
        Raises a DRF ValidationError (→ 400) with a per-item message on the
        first failure — never a raw 500.
        """
        org = actor.organization
        # user is unused when org is set (org actors have user=None); passed
        # for the shared validator signature.
        user = actor.user

        for idx, item in enumerate(media_data):
            allowed_extensions = RecruitmentService._media_extensions(
                item.get("media_type")
            )

            try:
                validate_media(
                    user=user,
                    url=item["file_url"],
                    public_id=item["public_id"],
                    org=org,
                    allowed_extensions=allowed_extensions
                )
            except ValueError as exc:
                raise RecruitmentService._media_error(idx, str(exc))

            # A poster frame is client-supplied now (nothing derives one
            # server-side), so it gets the same treatment as the file it belongs
            # to — our storage, an image extension, this org's own prefix, and
            # the same folder as the media it posters.
            thumbnail_url = item.get("thumbnail_url")
            if thumbnail_url:
                try:
                    validate_thumbnail(
                        user,
                        thumbnail_url,
                        parent_key=item["public_id"],
                        org=org,
                    )
                except ValueError:
                    raise ValidationError(
                        f"media[{idx}]: invalid thumbnail source"
                    )

    @staticmethod
    def _delete_orphaned_assets(public_ids):
        """
        Best-effort deletion of Cloudinary assets no longer referenced by the
        recruitment. Never raises — a failed cleanup must not break the request.
        Scheduled via transaction.on_commit so nothing is destroyed on rollback.
        """
        TAG = "RecruitmentService._delete_orphaned_assets"
        storage = get_storage_service()

        for public_id in public_ids:
            try:
                storage.delete_file(public_id)
            except Exception as exc:
                logger.warning(
                    f"{TAG} | Failed to delete asset | "
                    f"public_id={public_id} | {exc}"
                )

    # -----------------------------------------------------------------
    # NESTED CHILD HELPERS
    # Each helper deletes existing children for the recruitment and
    # bulk_creates from the incoming payload. On create the delete is a
    # no-op (no rows yet); on update it gives clean replace-on-update.
    # Shared by both create_recruitment and update_recruitment so the
    # child-building logic can never drift between the two paths.
    # EXCEPTION: _sync_age_categories diff-syncs instead, because applications
    # reference those rows — see its docstring.
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
        # Diff existing vs incoming BEFORE deleting rows so we can clean up the
        # Cloudinary assets that are no longer referenced (orphans). On create
        # there are no existing rows, so this set is empty.
        existing_public_ids = set(
            recruitment.media.values_list("public_id", flat=True)
        )
        incoming_public_ids = {
            media["public_id"]
            for media in media_data
            if media.get("public_id")
        }
        orphaned_public_ids = existing_public_ids - incoming_public_ids

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

        # Delete orphaned assets only AFTER the DB transaction commits, so files
        # are never destroyed if the transaction rolls back. Best-effort.
        if orphaned_public_ids:
            transaction.on_commit(
                lambda: RecruitmentService._delete_orphaned_assets(
                    orphaned_public_ids
                )
            )

    @staticmethod
    def _sync_age_categories(recruitment, age_categories_data):
        """
        DIFF sync, not the delete-and-recreate the sibling helpers use.

        Applications point at these rows (RecruitmentApplication.age_category),
        so recreating them on every edit would SET_NULL the group every
        applicant applied under — silent data loss on a rename. Instead:
        payload rows carrying an `id` are updated in place, rows without one
        are created, and rows the payload dropped are deleted (their
        applications correctly fall back to no group — it no longer exists).

        An `id` the recruitment does not own is rejected rather than adopted,
        so an edit can never steal another recruitment's group.
        """
        existing = {
            str(category.id): category
            for category in recruitment.age_categories.all()
        }

        seen_ids = set()
        to_create = []
        to_update = []

        for idx, age in enumerate(age_categories_data):
            raw_id = age.get("id")
            display_order = age.get("display_order", idx)

            if raw_id is None:
                to_create.append(
                    RecruitmentAgeCategory(
                        recruitment=recruitment,
                        title=age["title"],
                        min_birth_year=age.get(
                            "min_birth_year"
                        ),
                        max_birth_year=age.get(
                            "max_birth_year"
                        ),
                        reporting_time=age.get(
                            "reporting_time"
                        ),
                        display_order=display_order
                    )
                )
                continue

            category_id = str(raw_id)

            if category_id in seen_ids:
                raise ValidationError(
                    f"Duplicate age category id: {category_id}"
                )

            category = existing.get(category_id)
            if category is None:
                raise ValidationError(
                    f"Invalid age category id: {category_id}"
                )

            seen_ids.add(category_id)

            category.title = age["title"]
            category.min_birth_year = age.get("min_birth_year")
            category.max_birth_year = age.get("max_birth_year")
            category.reporting_time = age.get("reporting_time")
            category.display_order = display_order
            to_update.append(category)

        removed_ids = set(existing) - seen_ids
        if removed_ids:
            recruitment.age_categories.filter(
                id__in=removed_ids
            ).delete()

        if to_update:
            RecruitmentAgeCategory.objects.bulk_update(
                to_update,
                [
                    "title",
                    "min_birth_year",
                    "max_birth_year",
                    "reporting_time",
                    "display_order",
                ]
            )

        if to_create:
            RecruitmentAgeCategory.objects.bulk_create(
                to_create
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

    @staticmethod
    def _sync_eligibility_criteria(
        recruitment, eligibility_criteria_data
    ):
        # Plain replace-on-update: unlike age categories, nothing references
        # these rows, so recreating them costs nothing.
        recruitment.eligibility_criteria.all().delete()

        criteria_objs = [
            RecruitmentEligibilityCriteria(
                recruitment=recruitment,
                title=criteria["title"],
                display_order=criteria.get(
                    "display_order",
                    idx
                )
            )
            for idx, criteria
            in enumerate(eligibility_criteria_data)
        ]

        if criteria_objs:
            RecruitmentEligibilityCriteria.objects.bulk_create(
                criteria_objs
            )



