# recruitments/services/application_service.py
import logging
from django.db import transaction, IntegrityError
from django.db.models import F
from django.db.models.functions import Greatest
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from accounts.models import User
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

    # Statuses an org admin may set. Free transitions between these (no forward-
    # only pipeline). `withdrawn`/`applied` are never valid org targets, and
    # `invited` is reserved for the future personal-invite feature (not offered
    # in the org status UI/API).
    STATUS_CHANGE_TARGETS = {
        RecruitmentApplication.Status.REVIEWING,
        RecruitmentApplication.Status.SHORTLISTED,
        RecruitmentApplication.Status.SELECTED,
        RecruitmentApplication.Status.REJECTED,
    }

    # Max applications a single bulk-status request may touch.
    MAX_BULK_STATUS = 100

    # -----------------------------------------------------------------
    # APPLY (+ reapply)
    # -----------------------------------------------------------------
    @staticmethod
    @transaction.atomic
    def apply(actor, recruitment_id, validated_data):
        """
        Create — or, for a previously withdrawn applicant, REVIVE — a player's
        application to a recruitment.

        Locks the recruitment row so the eligibility re-checks (status /
        deadline / cap) are race-safe. A withdrawn row is reused (never a second
        row for the same recruitment+applicant), so the unique constraint can
        never be hit on reapply.
        """
        applicant = actor.user

        # Lock the recruitment row. Eligibility is re-evaluated against this
        # locked, freshly-read row (its applications_count already reflects any
        # prior withdraw's decrement, so the cap check is correct on reapply).
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

        # Reuse the existing row for this (recruitment, applicant), if any.
        existing = (
            RecruitmentApplication.objects
            .select_for_update()
            .filter(recruitment=recruitment, applicant=applicant)
            .first()
        )

        if (
            existing
            and existing.status != RecruitmentApplication.Status.WITHDRAWN
        ):
            raise ValidationError(
                "You have already applied to this recruitment."
            )

        if existing:
            # REAPPLY — revive the withdrawn row (keep notes + history audit).
            application = existing
            application.shared_name = validated_data["shared_name"]
            application.shared_email = validated_data.get("shared_email", "")
            application.shared_phone = validated_data["shared_phone"]
            application.status = RecruitmentApplication.Status.APPLIED
            application.reviewed_by = None
            application.reviewed_at = None
            # applied_at is auto_now_add; on UPDATE Django keeps whatever we set,
            # so re-stamping it here surfaces the reapply as the new apply time.
            application.applied_at = timezone.now()
            application.save(update_fields=[
                "shared_name", "shared_email", "shared_phone",
                "status", "reviewed_by", "reviewed_at", "applied_at",
            ])
            # Replace the old answers wholesale.
            application.answers.all().delete()
            history_from = RecruitmentApplication.Status.WITHDRAWN
            history_note = "Reapplied"
        else:
            # FRESH APPLY — the unique (recruitment, applicant) constraint is the
            # race-safe backstop against a double insert.
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
            history_from = ""
            history_note = ""

        # ANSWERS — one row per checkbox option; one row for text/number/single.
        answer_objs = ApplicationService._build_answer_objs(
            application, validated_data.get("answers", [])
        )
        if answer_objs:
            RecruitmentApplicationAnswer.objects.bulk_create(answer_objs)

        # DENORMALIZED COUNTER — atomic increment on the locked row.
        recruitment.applications_count = F("applications_count") + 1
        recruitment.save(update_fields=["applications_count"])

        # STATUS HISTORY — apply / reapply entry into the pipeline.
        RecruitmentApplicationStatusHistory.objects.create(
            application=application,
            from_status=history_from,
            to_status=RecruitmentApplication.Status.APPLIED,
            note=history_note,
        )

        # NOTIFY the owning org AFTER commit — a notification/FCM failure can
        # never fail or roll back the application. (The service dedups per
        # applicant+recruitment, so a reapply won't re-notify.)
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

    @staticmethod
    def _build_answer_objs(application, answers):
        """Shared by apply + reapply. Checkbox → one row per option."""
        objs = []
        for answer in answers:
            option_ids = answer.get("selected_option_ids") or []
            if option_ids:
                for option_id in option_ids:
                    objs.append(
                        RecruitmentApplicationAnswer(
                            application=application,
                            question_id=answer["question_id"],
                            answer_text="",
                            selected_option_id=option_id,
                        )
                    )
            else:
                objs.append(
                    RecruitmentApplicationAnswer(
                        application=application,
                        question_id=answer["question_id"],
                        answer_text=answer.get("answer_text", ""),
                    )
                )
        return objs

    # -----------------------------------------------------------------
    # WITHDRAW (player-owned)
    # -----------------------------------------------------------------
    @staticmethod
    @transaction.atomic
    def withdraw(actor, application_id):
        """
        Player withdraws their own application from ANY status except withdrawn.
        Decrements the recruitment counter (floored at 0) so a freed slot
        reopens the cap for others. Ownership is re-checked under the row lock.
        """
        applicant = actor.user

        application = (
            RecruitmentApplication.objects
            .select_for_update()
            .filter(id=application_id, applicant=applicant)
            .first()
        )
        if not application:
            raise ValidationError("Application not found.")

        if application.status == RecruitmentApplication.Status.WITHDRAWN:
            raise ValidationError("Application already withdrawn.")

        old_status = application.status
        application.status = RecruitmentApplication.Status.WITHDRAWN
        application.save(update_fields=["status"])

        RecruitmentApplicationStatusHistory.objects.create(
            application=application,
            from_status=old_status,
            to_status=RecruitmentApplication.Status.WITHDRAWN,
            changed_by=None,
            note="Withdrawn by applicant",
        )

        # Free the slot — atomic decrement with a hard floor at 0 (the row-level
        # UPDATE holds a lock for the duration, so concurrent withdraws are safe).
        Recruitment.objects.filter(id=application.recruitment_id).update(
            applications_count=Greatest(F("applications_count") - 1, 0)
        )

        return application

    # -----------------------------------------------------------------
    # ORG STATUS CHANGE (single + bulk, one code path)
    # -----------------------------------------------------------------
    @staticmethod
    @transaction.atomic
    def change_status(actor, recruitment, application_ids, to_status, note=""):
        """
        Org moves 1..N applications of `recruitment` to `to_status`. Free
        transitions between {reviewing, shortlisted, invited, selected,
        rejected}. Partial success: each id is validated independently and
        invalid ones are skipped (never fail the whole batch).

        Returns {"updated": [ids], "skipped": [{"id", "reason"}]}.
        `recruitment` is already ownership-gated by the caller.
        """
        if to_status not in ApplicationService.STATUS_CHANGE_TARGETS:
            raise ValidationError("Invalid target status.")

        if len(application_ids) > ApplicationService.MAX_BULK_STATUS:
            raise ValidationError(
                f"Cannot update more than "
                f"{ApplicationService.MAX_BULK_STATUS} applications at once."
            )

        member = actor.organization_member
        now = timezone.now()

        locked = (
            RecruitmentApplication.objects
            .select_for_update()
            .filter(id__in=application_ids, recruitment=recruitment)
        )
        apps_by_id = {str(app.id): app for app in locked}

        updated = []
        skipped = []
        to_update = []
        history_rows = []
        notify_apps = []

        for raw_id in application_ids:
            app_id = str(raw_id)
            app = apps_by_id.get(app_id)

            if app is None:
                skipped.append({"id": app_id, "reason": "not_found"})
                continue
            if app.status == RecruitmentApplication.Status.WITHDRAWN:
                skipped.append({"id": app_id, "reason": "withdrawn"})
                continue
            if app.status == to_status:
                skipped.append({"id": app_id, "reason": "no_change"})
                continue

            old_status = app.status
            app.status = to_status
            app.reviewed_by = member
            app.reviewed_at = now

            to_update.append(app)
            history_rows.append(
                RecruitmentApplicationStatusHistory(
                    application=app,
                    from_status=old_status,
                    to_status=to_status,
                    changed_by=member,
                    note=note,
                )
            )
            notify_apps.append(app)
            updated.append(app_id)

        if to_update:
            RecruitmentApplication.objects.bulk_update(
                to_update, ["status", "reviewed_by", "reviewed_at"]
            )
        if history_rows:
            RecruitmentApplicationStatusHistory.objects.bulk_create(history_rows)

        # NOTIFY each affected applicant AFTER commit. Prefetch applicants in one
        # query, then schedule a single callback that guards every send.
        if notify_apps:
            org = recruitment.organization
            applicants = User.objects.in_bulk(
                [app.applicant_id for app in notify_apps]
            )
            notify_data = [
                (app.id, applicants.get(app.applicant_id))
                for app in notify_apps
            ]

            # Selection additionally invites the applicant to turn the result
            # into a career entry. It rides on this same status change rather
            # than a parallel flow, so there is exactly one place a selection
            # happens. The prompt is deduplicated per application inside the
            # notification service.
            prompt_career_add = (
                to_status == RecruitmentApplication.Status.SELECTED
            )

            def _notify_applicants():
                for application_id, applicant in notify_data:
                    if applicant is None:
                        continue
                    try:
                        NotificationService.recruitment_application_status(
                            actor_org=org,
                            recipient_user=applicant,
                            recruitment=recruitment,
                            to_status=to_status,
                            application_id=application_id,
                        )
                    except Exception as exc:
                        logger.warning(
                            "ApplicationService.change_status | notification "
                            f"failed | application_id={application_id} | {exc}"
                        )

                    if not prompt_career_add:
                        continue

                    # Guarded separately: the status notification is the one the
                    # applicant must get, and a failure here must not cost them
                    # that. Both are already outside the transaction.
                    try:
                        NotificationService.career_add_prompt(
                            actor_org=org,
                            recipient_user=applicant,
                            recruitment=recruitment,
                            application_id=application_id,
                        )
                    except Exception as exc:
                        logger.warning(
                            "ApplicationService.change_status | career prompt "
                            f"failed | application_id={application_id} | {exc}"
                        )

            transaction.on_commit(_notify_applicants)

        return {"updated": updated, "skipped": skipped}
