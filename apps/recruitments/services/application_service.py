# recruitments/services/application_service.py
import logging
from zoneinfo import ZoneInfo
from django.conf import settings
from django.db import transaction, IntegrityError
from django.db.models import F
from django.db.models.functions import Greatest
from django.utils import dateformat, timezone
from rest_framework.exceptions import ValidationError
from apps.accounts.models import User
from apps.recruitments.models import (
    Recruitment,
    RecruitmentApplication,
    RecruitmentApplicationAnswer,
    RecruitmentApplicationStatusHistory,
)
from apps.recruitments.pass_code import mint_for
from apps.recruitments.legacy_status import (
    LEGACY_STATUSES,
    local_date,
    map_legacy_status,
)
from apps.recruitments.trial_window import is_trial_over, start_of_today
from apps.sports.models import SportPosition
from apps.connections.services.follow_services import FollowService
from core.constant import TYPE_ORGANIZATION
from apps.notifications.services.notification_service import NotificationService
from apps.moderation.services.block_guard import require_not_blocked
from apps.recruitments.services.applicant_alert_service import (
    should_send_applicant_alert,
)
from apps.recruitments.services.eligibility_service import is_age_mismatch
from utils.transactional_emails import (
    send_application_received_email,
    send_application_status_email,
    send_new_applicant_alert_email,
)


logger = logging.getLogger(__name__)


class ApplicationService:

    # Statuses an org admin may set. Free transitions between these (no forward-
    # only pipeline). `withdrawn`/`applied` are never valid org targets.
    # `invited` and `rejected` are still valid VALUES on the model (older rows
    # carry them) but are no longer settable: `rejected` split into the honest
    # `not_shortlisted` / `not_selected`, and `invited` gave way to
    # `trial_confirmed`.
    STATUS_CHANGE_TARGETS = {
        RecruitmentApplication.Status.REVIEWING,
        RecruitmentApplication.Status.SHORTLISTED,
        RecruitmentApplication.Status.TRIAL_CONFIRMED,
        RecruitmentApplication.Status.NOT_SHORTLISTED,
        RecruitmentApplication.Status.SELECTED,
        RecruitmentApplication.Status.NOT_SELECTED,
    }

    # The org's private working states: moving an application into one of
    # these sends the applicant nothing — no in-app row, no push, no email.
    SILENT_STATUSES = {
        RecruitmentApplication.Status.REVIEWING,
        RecruitmentApplication.Status.SHORTLISTED,
    }

    # Trial results. On an open trial these open on the trial day, not before.
    RESULT_STATUSES = {
        RecruitmentApplication.Status.SELECTED,
        RecruitmentApplication.Status.NOT_SELECTED,
    }

    # Recruitment counters that track how many applications sit in a status.
    # Written only here: change_status moves them, withdraw gives them back.
    _STATUS_COUNTERS = {
        RecruitmentApplication.Status.TRIAL_CONFIRMED: "confirmed_count",
        RecruitmentApplication.Status.SELECTED: "selected_count",
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

        # BLOCK GUARD — either direction between the applicant and the posting
        # org. Ahead of the eligibility rules so a blocked applicant is never
        # told whether the deadline passed or the cap was hit. Raises
        # BlockedError (403).
        require_not_blocked(actor, recruitment.organization)

        # ELIGIBILITY (authoritative, under the row lock)
        if recruitment.status != Recruitment.Status.ACTIVE:
            raise ValidationError(
                "This recruitment is not accepting applications."
            )

        # THE APPLICATION WINDOW — ``applications_close_at``, never
        # ``is_trial_over``. The two are different questions with different
        # answers: on an "attend every date" trial applications close on the
        # FIRST date while the trial itself runs on for days. The message
        # has to keep them apart too, or a player told "this trial has
        # ended" on a Saturday turns up on the Sunday.
        closes_at = recruitment.applications_close_at
        if closes_at and closes_at < timezone.now():
            if recruitment.is_trial_over:
                raise ValidationError("This trial has ended.")

            if (
                recruitment.application_deadline
                and recruitment.application_deadline <= closes_at
            ):
                # The org's own deadline is what bound, so say so.
                raise ValidationError(
                    "The application deadline has passed."
                )

            raise ValidationError(
                "Applications for this trial have closed."
            )

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

        # The age group the applicant chose, already validated as belonging to
        # this recruitment by the serializer. Never enforced against their
        # birthdate — verification happens at the venue — but a mismatch is
        # recorded so the org can see it.
        age_category_id = validated_data.get("age_category")
        age_mismatch = ApplicationService._age_mismatch(
            recruitment, applicant, age_category_id
        )

        # AUTO-CONFIRM — the org said yes in advance, so the application
        # lands confirmed instead of applied and the applicant gets their
        # pass immediately. It does NOT widen the window: the
        # applications_close_at gate above already ran, which is exactly
        # the pairing the two windows were separated for.
        auto_confirm = (
            recruitment.recruitment_type == Recruitment.Type.OPEN_TRIAL
            and recruitment.auto_confirm
        )
        landing_status = (
            RecruitmentApplication.Status.TRIAL_CONFIRMED
            if auto_confirm
            else RecruitmentApplication.Status.APPLIED
        )

        # The date they are coming to, on a choose_one trial. Resolved
        # under the row lock taken above, so a date cancelled while the
        # form was open is caught here and not stored.
        session_id = ApplicationService._resolve_session(
            recruitment, validated_data.get("session")
        )

        if existing:
            # REAPPLY — revive the withdrawn row (keep notes + history audit).
            application = existing
            application.shared_name = validated_data["shared_name"]
            application.shared_email = validated_data.get("shared_email", "")
            application.shared_phone = validated_data["shared_phone"]
            application.age_category_id = age_category_id
            application.session_id = session_id
            # Recomputed: the reapply may be under a different group.
            application.age_mismatch_at_apply = age_mismatch
            application.status = landing_status
            # Auto-confirm lands them confirmed, so they get their code
            # here — the same helper change_status calls.
            if landing_status == RecruitmentApplication.Status.TRIAL_CONFIRMED:
                mint_for(application, save=False)
            application.reviewed_by = None
            application.reviewed_at = None
            # applied_at is auto_now_add; on UPDATE Django keeps whatever we set,
            # so re-stamping it here surfaces the reapply as the new apply time.
            application.applied_at = timezone.now()
            application.save(update_fields=[
                "shared_name", "shared_email", "shared_phone",
                "age_category", "session", "age_mismatch_at_apply",
                "status", "pass_code",
                "reviewed_by", "reviewed_at", "applied_at",
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
                    age_category_id=age_category_id,
                    session_id=session_id,
                    age_mismatch_at_apply=age_mismatch,
                    status=landing_status,
                )
            except IntegrityError:
                raise ValidationError(
                    "You have already applied to this recruitment."
                )
            # A fresh auto-confirmed application needs its code too. The
            # row exists by now, so this one saves itself.
            if landing_status == RecruitmentApplication.Status.TRIAL_CONFIRMED:
                mint_for(application)

            history_from = ""
            history_note = ""

        # ANSWERS — one row per checkbox option; one row for text/number/single.
        answer_objs = ApplicationService._build_answer_objs(
            application, validated_data.get("answers", [])
        )
        if answer_objs:
            RecruitmentApplicationAnswer.objects.bulk_create(answer_objs)

        # DENORMALIZED COUNTERS — atomic increments on the locked row.
        # An auto-confirmed application enters `trial_confirmed` here and
        # nowhere else, so confirmed_count has to move with it or the
        # org's Confirmed tab and its number disagree.
        recruitment.applications_count = F("applications_count") + 1
        counter_fields = ["applications_count"]
        if auto_confirm:
            recruitment.confirmed_count = F("confirmed_count") + 1
            counter_fields.append("confirmed_count")
        recruitment.save(update_fields=counter_fields)

        # STATUS HISTORY — apply / reapply entry into the pipeline. An
        # auto-confirm is a real move and is recorded as one, with no
        # changed_by: nobody pressed anything.
        RecruitmentApplicationStatusHistory.objects.create(
            application=application,
            from_status=history_from,
            to_status=landing_status,
            changed_by=None,
            note="auto-confirmed" if auto_confirm else history_note,
        )

        # APPLICANT ALERT — decided here, INSIDE the transaction and under the
        # recruitment row lock taken at the top of this method. Two people
        # applying at the same instant would otherwise both read the same
        # `last_applicant_alert_at`, both decide the gap was up, and both send.
        # The lock serializes them, so the second one reads the stamp the first
        # just wrote and stays quiet.
        alert_payload = ApplicationService._claim_applicant_alert(
            recruitment, application
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

            # Email is ADDITIONAL to the in-app/FCM notification above, never a
            # replacement, and is guarded separately so a mail problem cannot
            # cost the org the notification it already earned.
            try:
                send_application_received_email(application=application)
            except Exception as exc:
                logger.warning(
                    "ApplicationService.apply | received email failed | "
                    f"application_id={application.id} | {exc}"
                )

            if alert_payload is None:
                return

            try:
                send_new_applicant_alert_email(
                    recruitment=recruitment,
                    latest_application=application,
                    **alert_payload,
                )
            except Exception as exc:
                logger.warning(
                    "ApplicationService.apply | applicant alert failed | "
                    f"recruitment_id={recruitment.id} | {exc}"
                )

        transaction.on_commit(_notify_org)

        # ...and, when it auto-confirmed, tell the APPLICANT — the same
        # notification and the same email a member pressing Confirm would
        # have sent. Separate callback so a failure here cannot cost the
        # org its new-applicant alert, or the other way round.
        if auto_confirm:
            org = recruitment.organization
            application.recruitment = recruitment
            application.applicant = applicant

            def _notify_auto_confirmed():
                try:
                    NotificationService.recruitment_application_status(
                        actor_org=org,
                        recipient_user=applicant,
                        recruitment=recruitment,
                        to_status=(
                            RecruitmentApplication.Status.TRIAL_CONFIRMED
                        ),
                        application_id=application.id,
                    )
                except Exception as exc:
                    logger.warning(
                        "ApplicationService.apply | auto-confirm "
                        f"notification failed | "
                        f"application_id={application.id} | {exc}"
                    )

                try:
                    send_application_status_email(
                        application=application,
                        to_status=(
                            RecruitmentApplication.Status.TRIAL_CONFIRMED
                        ),
                    )
                except Exception as exc:
                    logger.warning(
                        "ApplicationService.apply | auto-confirm email "
                        f"failed | application_id={application.id} | {exc}"
                    )

            transaction.on_commit(_notify_auto_confirmed)

        return application

    @staticmethod
    def _claim_applicant_alert(recruitment, application):
        """Decide whether THIS apply gets to send the org an alert email.

        Returns the alert's counts when it wins the slot, else None. "Claim" is
        the point: on a True decision it stamps `last_applicant_alert_at`
        immediately, inside the transaction, so a concurrent apply waiting on
        the same row lock sees a fresh stamp and stands down.

        Must be called with the recruitment row already locked — apply() takes
        that lock for its eligibility checks and this rides on it.

        Counts exclude `withdrawn`: a withdrawn application is not somebody the
        org can review, so counting it would inflate both the tier the
        recruitment sits in and the number quoted in the mail.

        Accepted trade-off: the stamp advances even if the post-commit send
        later fails, making delivery at-most-once. The alternative — stamping
        after a successful send — reopens the double-send race the lock exists
        to close, and a missed alert costs less than a duplicate one. Every
        application also produced its own in-app/FCM notification regardless.
        """
        live_applications = (
            RecruitmentApplication.objects
            .filter(recruitment=recruitment)
            .exclude(status=RecruitmentApplication.Status.WITHDRAWN)
        )

        total_count = live_applications.count()
        last_alert_at = recruitment.last_applicant_alert_at

        # Applications that arrived during a quiet gap are counted into THIS
        # alert rather than dropped — that is what makes the rollup honest.
        new_count = (
            total_count
            if last_alert_at is None
            else live_applications.filter(applied_at__gt=last_alert_at).count()
        )

        now = timezone.now()
        if not should_send_applicant_alert(
            total_count=total_count,
            last_alert_at=last_alert_at,
            now=now,
            tiers=settings.APPLICANT_ALERT_TIERS,
        ):
            return None

        recruitment.last_applicant_alert_at = now
        recruitment.save(update_fields=["last_applicant_alert_at"])

        return {"new_count": new_count, "total_count": total_count}

    @staticmethod
    def _resolve_session(recruitment, session_id):
        """
        The trial date the applicant picked, as an id to store — or None.

        Only a choose_one open trial has a date to pick. In `all` mode every
        date is part of the one trial, and on every other posting there are no
        dates at all, so a session sent for either is IGNORED rather than
        stored: the client must not be able to stamp an application with a
        date the org never offered as a choice.

        A date from another recruitment is rejected, never adopted — the same
        belongs-to-THIS-recruitment rule the age group gets in the serializer.
        """
        if (
            recruitment.recruitment_type != Recruitment.Type.OPEN_TRIAL
            or recruitment.session_mode != Recruitment.SessionMode.CHOOSE_ONE
        ):
            return None

        if session_id is None:
            raise ValidationError("Pick which date you'll attend.")

        session = recruitment.sessions.filter(id=session_id).first()
        if session is None:
            raise ValidationError("Invalid date for this recruitment.")

        if session.is_cancelled:
            raise ValidationError(
                "That date has been cancelled. Pick another."
            )

        # By calendar day in RECRUITMENT_TIMEZONE, like every other date rule
        # here: today's date is still pickable all day.
        if session.date < start_of_today().date():
            raise ValidationError("That date has passed. Pick another.")

        return session.id

    @staticmethod
    def _age_mismatch(recruitment, applicant, age_category_id):
        """Whether the profile birth year sits outside the chosen group.

        Server-side only — the client never gets a say. No group chosen means
        no query at all.
        """
        if age_category_id is None:
            return False

        category = (
            recruitment.age_categories.filter(id=age_category_id).first()
        )
        profile = getattr(applicant, "profile", None)
        birthdate = getattr(profile, "birthdate", None)

        return is_age_mismatch(
            category, birthdate.year if birthdate else None
        )

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
        # A confirmed or selected player leaving takes their count with them,
        # in the same statement.
        counters = {"applications_count": -1}
        counter = ApplicationService._STATUS_COUNTERS.get(old_status)
        if counter:
            counters[counter] = -1
        Recruitment.objects.filter(id=application.recruitment_id).update(
            **ApplicationService._counter_updates(counters)
        )

        return application

    @staticmethod
    def _counter_updates(deltas):
        """{field: delta} → .update() kwargs, one F() expression per counter.

        Decrements are floored at 0 with Greatest, same as applications_count
        always was, so a counter that drifted low can never go negative.
        """
        updates = {}
        for field, delta in deltas.items():
            if delta > 0:
                updates[field] = F(field) + delta
            elif delta < 0:
                updates[field] = Greatest(F(field) - abs(delta), 0)
        return updates

    # -----------------------------------------------------------------
    # ORG STATUS CHANGE (single + bulk, one code path)
    # -----------------------------------------------------------------
    @staticmethod
    @transaction.atomic
    def change_status(actor, recruitment, application_ids, to_status, note=""):
        """
        Org moves 1..N applications of `recruitment` to `to_status`. Free
        transitions between STATUS_CHANGE_TARGETS. Partial success: each id is
        validated independently and invalid ones are skipped (never fail the
        whole batch). The open-trial date guards are the exception — they are
        about the recruitment, not an application, so they refuse the batch.

        Moves confirmed_count / selected_count in the same transaction, and
        notifies nobody for the silent statuses (reviewing, shortlisted).

        Returns {"updated": [ids], "skipped": [{"id", "reason"}]}.
        `recruitment` is already ownership-gated by the caller.
        """
        # A STALE CLIENT'S WORDS, TRANSLATED. Goatza is an installed PWA,
        # so old JavaScript lives on phones for days after a deploy and
        # still POSTs `invited` or `rejected`. Without this the target
        # check below answers "Invalid target status." and the org's
        # decision is silently lost. Runs BEFORE that check, so the
        # mapped value is what gets validated.
        to_status = ApplicationService._map_legacy_target(
            recruitment, to_status
        )

        if to_status not in ApplicationService.STATUS_CHANGE_TARGETS:
            raise ValidationError("Invalid target status.")

        if len(application_ids) > ApplicationService.MAX_BULK_STATUS:
            raise ValidationError(
                f"Cannot update more than "
                f"{ApplicationService.MAX_BULK_STATUS} applications at once."
            )

        ApplicationService._check_trial_window(recruitment, to_status)

        # The org's private working states — the applicant hears nothing.
        silent = to_status in ApplicationService.SILENT_STATUSES

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
        counter_deltas = {}

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

            # THE PASS'S BOOKING REFERENCE, minted at the one place a
            # status is written. Idempotent, so a round-trip out of
            # trial_confirmed and back keeps the code the player already
            # screenshotted.
            if to_status == RecruitmentApplication.Status.TRIAL_CONFIRMED:
                mint_for(app, save=False)

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
            # Leaving a counted status gives one back; entering one takes one.
            for status, delta in ((old_status, -1), (to_status, 1)):
                field = ApplicationService._STATUS_COUNTERS.get(status)
                if field:
                    counter_deltas[field] = counter_deltas.get(field, 0) + delta
            if not silent:
                notify_apps.append(app)
            updated.append(app_id)

        if to_update:
            RecruitmentApplication.objects.bulk_update(
                to_update,
                ["status", "reviewed_by", "reviewed_at", "pass_code"],
            )
        if history_rows:
            RecruitmentApplicationStatusHistory.objects.bulk_create(history_rows)

        # DENORMALIZED COUNTERS — the whole batch's net move, one statement,
        # inside this transaction so they can never disagree with the rows.
        counter_updates = ApplicationService._counter_updates(counter_deltas)
        if counter_updates:
            Recruitment.objects.filter(id=recruitment.id).update(
                **counter_updates
            )

        # NOTIFY each affected applicant AFTER commit. Prefetch applicants in one
        # query, then schedule a single callback that guards every send.
        if notify_apps:
            org = recruitment.organization
            applicants = User.objects.in_bulk(
                [app.applicant_id for app in notify_apps]
            )
            # One query for the positions the status email's card needs,
            # instead of one per applicant inside the loop below. They cannot
            # ride on the select_for_update() above: applied_position is
            # nullable, and Postgres refuses FOR UPDATE across an outer join.
            positions = SportPosition.objects.in_bulk(
                [
                    app.applied_position_id for app in notify_apps
                    if app.applied_position_id
                ]
            )

            # Pre-populate the relation caches the email reads, so nothing in
            # the post-commit loop touches the database.
            for app in notify_apps:
                app.recruitment = recruitment
                app.applicant = applicants.get(app.applicant_id)
                app.applied_position = positions.get(app.applied_position_id)

            notify_data = [
                (app.id, applicants.get(app.applicant_id), app)
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
                for application_id, applicant, application in notify_data:
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

                    # Email ALONGSIDE the notification, guarded on its own so
                    # one bad address cannot end the loop and leave the rest of
                    # a 100-application batch unnotified. The sender ignores
                    # statuses that are not worth an email, so no filtering
                    # here.
                    try:
                        send_application_status_email(
                            application=application,
                            to_status=to_status,
                        )
                    except Exception as exc:
                        logger.warning(
                            "ApplicationService.change_status | status email "
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

    @staticmethod
    def _map_legacy_target(recruitment, to_status):
        """
        Translate a retired status value into the one that replaced it.

        The rule itself lives in ``legacy_status`` because
        ``migrate_recruitment_v3`` applies the SAME rule to historical rows —
        two implementations would eventually classify the same application
        two different ways.

        Everything current passes straight through, so this is a no-op on
        every request from an up-to-date client.
        """
        if to_status not in LEGACY_STATUSES:
            return to_status

        mapped = map_legacy_status(
            to_status,
            recruitment_type=recruitment.recruitment_type,
            # The same helper the results guard reads, so "has the trial
            # started" has one answer on this code path.
            trial_starts_at=ApplicationService._trial_starts_at(recruitment),
            decided_on=local_date(timezone.now()),
        )

        # INFO, not DEBUG: this line is the signal for whether it is safe to
        # remove the legacy values from the choices. When it stops appearing
        # in production logs, every client has reloaded.
        #
        #     grep "legacy status mapped" <logs>
        logger.info(
            "ApplicationService | legacy status mapped | sent=%s | "
            "mapped_to=%s | recruitment_id=%s",
            to_status, mapped, recruitment.id,
        )

        return mapped

    # -----------------------------------------------------------------
    # TRIAL FEE (single + bulk, one code path)
    # -----------------------------------------------------------------
    @staticmethod
    @transaction.atomic
    def set_fee_paid(actor, recruitment, application_ids, fee_paid):
        """
        Record whether the trial fee was collected, for 1..N applications of
        `recruitment`. Any org member may — it is the person on the gate who
        knows, not the admin.

        Partial success, the same shape change_status returns: each id is
        validated on its own and invalid ones are skipped rather than failing
        the batch.

        Fee status NEVER gates confirmation or selection. Nothing anywhere
        reads these columns to decide anything; they are information the org
        keeps for itself.

        Returns {"updated": [ids], "skipped": [{"id", "reason"}]}.
        `recruitment` is already ownership-gated by the caller.
        """
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

        for raw_id in application_ids:
            app_id = str(raw_id)
            app = apps_by_id.get(app_id)

            if app is None:
                skipped.append({"id": app_id, "reason": "not_found"})
                continue
            if app.status == RecruitmentApplication.Status.WITHDRAWN:
                skipped.append({"id": app_id, "reason": "withdrawn"})
                continue
            if app.fee_paid == fee_paid:
                skipped.append({"id": app_id, "reason": "no_change"})
                continue

            app.fee_paid = fee_paid
            # Unmarking clears the provenance too — a stamp left behind would
            # read as "collected, then refunded", which is a different story
            # from "marked by mistake".
            app.fee_paid_at = now if fee_paid else None
            app.fee_marked_by = member if fee_paid else None

            to_update.append(app)
            updated.append(app_id)

        if to_update:
            RecruitmentApplication.objects.bulk_update(
                to_update, ["fee_paid", "fee_paid_at", "fee_marked_by"]
            )

        return {"updated": updated, "skipped": skipped}

    # -----------------------------------------------------------------
    # OPEN-TRIAL DATE GUARDS
    # -----------------------------------------------------------------

    @staticmethod
    def _trial_starts_at(recruitment):
        """
        The trial's first day — results open on it. None: no boundary.

        Read from the sessions rather than the cached event_date so a
        recruitment written before the backfill, or one whose window has
        drifted, still answers from the dates themselves.
        """
        session = (
            recruitment.sessions
            .filter(is_cancelled=False)
            .order_by("date", "start_time", "display_order")
            .first()
        )
        if session is None:
            return recruitment.event_date
        return session.starts_at

    @staticmethod
    def _trial_ends_at(recruitment):
        """The trial's last day — it is over once that day ends."""
        return recruitment.trial_end_date or recruitment.event_date

    @staticmethod
    def _check_trial_window(recruitment, to_status):
        """
        Refuse a status the trial's dates make dishonest. Recruitment-level,
        so it raises for the whole batch rather than skipping per application.
        Only open trials have a trial day; every other type passes untouched.
        """
        if recruitment.recruitment_type != Recruitment.Type.OPEN_TRIAL:
            return

        if to_status in ApplicationService.RESULT_STATUSES:
            starts_at = ApplicationService._trial_starts_at(recruitment)
            today = start_of_today()
            # By calendar day in RECRUITMENT_TIMEZONE, like the trial-over
            # rule: results open on the trial day itself, whatever its time.
            if (
                starts_at is not None
                and starts_at.astimezone(today.tzinfo).date() > today.date()
            ):
                raise ValidationError(
                    f"Results open on {_trial_day_label(starts_at)}. "
                    "If that date is wrong, edit the trial."
                )

        if (
            to_status == RecruitmentApplication.Status.TRIAL_CONFIRMED
            and is_trial_over(ApplicationService._trial_ends_at(recruitment))
        ):
            raise ValidationError(
                "This trial has ended — you can no longer confirm players "
                "for it."
            )


def _trial_day_label(value):
    """"Sun 12 Oct" — a trial day as the org would say it, in its timezone."""
    local = value.astimezone(ZoneInfo(settings.RECRUITMENT_TIMEZONE))
    return dateformat.format(local, "D j M")
