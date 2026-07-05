# recruitments/services/application_service.py
import logging
from django.db import transaction, IntegrityError
from django.db.models import F
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from recruitments.models import (
    Recruitment,
    RecruitmentApplication,
    RecruitmentApplicationAnswer,
    RecruitmentApplicationStatusHistory,
)
from connections.services.follow_services import FollowService
from core.constant import TYPE_ORGANIZATION
from notifications.services.notification_service import NotificationService


logger = logging.getLogger(__name__)


class ApplicationService:

    @staticmethod
    @transaction.atomic
    def apply(actor, recruitment_id, validated_data):
        """
        Create a player's application to a recruitment.

        Runs inside a transaction and locks the recruitment row so the
        eligibility re-checks (status / deadline / cap) are race-safe — two
        simultaneous applies can never push applications_count past
        max_applications.
        """
        applicant = actor.user

        # Lock the recruitment row. All eligibility gates below are re-evaluated
        # against this locked, freshly-read row so nothing can change under us.
        recruitment = (
            Recruitment.objects
            .select_for_update()
            .filter(id=recruitment_id, is_deleted=False)
            .first()
        )
        if not recruitment:
            raise ValidationError("Recruitment not found.")

        # ELIGIBILITY (authoritative, under the row lock)
        if recruitment.status != Recruitment.Status.ACTIVE:
            raise ValidationError(
                "This recruitment is not accepting applications."
            )

        if (
            recruitment.application_deadline
            and recruitment.application_deadline < timezone.now()
        ):
            raise ValidationError("The application deadline has passed.")

        if (
            recruitment.max_applications is not None
            and recruitment.applications_count >= recruitment.max_applications
        ):
            raise ValidationError(
                "This recruitment has reached its application limit."
            )

        # VISIBILITY
        # followers_only → the applicant must follow the org.
        # private → applicants can never apply (mirrors the detail selector,
        # which only ever exposes a private recruitment to its owner org).
        if recruitment.visibility == Recruitment.Visibility.FOLLOWERS_ONLY:
            relationship = FollowService.get_relationship(
                actor=actor,
                target_id=recruitment.organization_id,
                target_type=TYPE_ORGANIZATION,
            )
            if not relationship["is_following"]:
                raise ValidationError(
                    "You must follow this organization to apply."
                )
        elif recruitment.visibility == Recruitment.Visibility.PRIVATE:
            raise ValidationError(
                "This recruitment is not open for applications."
            )

        # CREATE APPLICATION
        # The unique (recruitment, applicant) constraint is the race-safe guard
        # against double applies; catch it and surface a clean message.
        try:
            application = RecruitmentApplication.objects.create(
                recruitment=recruitment,
                applicant=applicant,
                shared_name=validated_data["shared_name"],
                shared_email=validated_data.get("shared_email", ""),
                shared_phone=validated_data["shared_phone"],
            )
        except IntegrityError:
            raise ValidationError(
                "You have already applied to this recruitment."
            )

        # ANSWERS
        # For checkbox questions the model stores one row per selected option
        # (single selected_option FK); text/number/single-choice → one row.
        answer_objs = []
        for answer in validated_data.get("answers", []):
            option_ids = answer.get("selected_option_ids") or []

            if option_ids:
                for option_id in option_ids:
                    answer_objs.append(
                        RecruitmentApplicationAnswer(
                            application=application,
                            question_id=answer["question_id"],
                            answer_text="",
                            selected_option_id=option_id,
                        )
                    )
            else:
                answer_objs.append(
                    RecruitmentApplicationAnswer(
                        application=application,
                        question_id=answer["question_id"],
                        answer_text=answer.get("answer_text", ""),
                    )
                )

        if answer_objs:
            RecruitmentApplicationAnswer.objects.bulk_create(answer_objs)

        # DENORMALIZED COUNTER — atomic increment on the locked row.
        recruitment.applications_count = F("applications_count") + 1
        recruitment.save(update_fields=["applications_count"])

        # STATUS HISTORY — first entry into the pipeline.
        RecruitmentApplicationStatusHistory.objects.create(
            application=application,
            from_status="",
            to_status=RecruitmentApplication.Status.APPLIED,
        )

        # NOTIFY the owning org — only AFTER the apply transaction commits, so a
        # notification/FCM failure can never fail or roll back the application.
        def _notify_org():
            try:
                NotificationService.recruitment_application(
                    actor_user=applicant,
                    recruitment=recruitment,
                )
            except Exception as exc:
                logger.warning(
                    "ApplicationService.apply | notification failed | "
                    f"application_id={application.id} | {exc}"
                )

        transaction.on_commit(_notify_org)

        return application
