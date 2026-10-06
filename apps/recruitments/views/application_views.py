import logging
from rest_framework import status
from rest_framework.exceptions import PermissionDenied, ValidationError
from core.views.base_views import BaseAPIView
from core.decorators.actor_required import player_required, org_required
from apps.recruitments.models import Recruitment, RecruitmentApplication
from apps.recruitments.selectors.recruitment_selectors import RecruitmentSelector
from apps.recruitments.selectors.application_selectors import ApplicationSelector
from apps.recruitments.serializers.application_serializers import (
    RecruitmentApplySerializer,
    ApplicantListItemSerializer,
    ApplicationDetailSerializer,
    BulkApplicationStatusSerializer,
    SingleApplicationStatusSerializer,
    MyApplicationListSerializer,
    ApplicationFeeSerializer,
    BulkApplicationFeeSerializer,
    MessageApplicantsSerializer,
    TrialFeedbackSerializer,
)
from apps.recruitments.feedback_window import (
    status_allows_feedback,
    trial_is_over,
)
from apps.recruitments.throttles import TrialFeedbackThrottle
from apps.highlights.selectors.highlight_selectors import (
    visible_highlight_counts_for,
)
from apps.recruitments.selectors.trial_pass_selectors import build_trial_pass
from apps.recruitments.services.announcement_service import AnnouncementService
from apps.recruitments.services.application_service import ApplicationService
from apps.recruitments.services.trial_feedback_service import (
    TrialFeedbackService,
)
from apps.moderation.services.block_guard import BlockedError, blocked_response
from utils.response import response_data
from utils.errors import flatten_validation_error


logger = logging.getLogger(__name__)


class ApplyRecruitmentAPIView(BaseAPIView):

    @player_required
    def post(self, request, recruitment_id):
        TAG = "ApplyRecruitmentAPIView"
        try:
            actor = request.actor

            # Existence check only — visibility/status/deadline/cap are enforced
            # (under a row lock) by the service. 404 for missing OR soft-deleted
            # so we never leak that a deleted recruitment existed.
            recruitment = (
                RecruitmentSelector.get_recruitment_for_apply(
                    recruitment_id=recruitment_id
                )
            )
            if not recruitment:
                return response_data(
                    success=False,
                    message="Recruitment not found",
                    status_code=status.HTTP_404_NOT_FOUND
                )

            serializer = RecruitmentApplySerializer(
                data=request.data,
                context={
                    "request": request,
                    "recruitment": recruitment
                }
            )
            serializer.is_valid(raise_exception=True)

            application = ApplicationService.apply(
                actor=actor,
                recruitment_id=recruitment.id,
                validated_data=serializer.validated_data
            )

            logger.info(
                f"{TAG} | Application created | "
                f"application_id={application.id} | "
                f"recruitment_id={recruitment.id}"
            )

            return response_data(
                success=True,
                message="Application submitted successfully",
                data={
                    "application_id": str(application.id),
                    "status": application.status,
                    "applied_at": application.applied_at.isoformat()
                }
            )

        except BlockedError:
            # Error MAPPING only — the guard itself lives in the service. Without
            # this branch the broad handler below turns a 403 into a 500.
            return blocked_response()

        except ValidationError as e:
            flat = flatten_validation_error(e.detail)
            logger.warning(
                f"{TAG} | Validation Error | {flat['message']}"
            )
            return response_data(
                success=False,
                message=flat["message"],
                status_code=400,
                error=flat["message"],
                data={"errors": flat["errors"]}
            )

        except Exception as e:
            logger.error(
                f"{TAG} | Error | {str(e)}"
            )
            return response_data(
                success=False,
                message="Something went wrong",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                error=str(e)
            )


class ListRecruitmentApplicationsAPIView(BaseAPIView):

    @org_required
    def get(self, request, recruitment_id):
        TAG = "ListRecruitmentApplicationsAPIView"
        try:
            actor = request.actor

            # One status, a comma-separated list, or the key repeated — all
            # arrive at the selector as one comma-separated string.
            status_filter = ",".join(request.query_params.getlist("status"))
            search = request.query_params.get("search", "").strip()
            age_category = request.query_params.get("age_category")

            # The fee, and the two AGE questions this list answers. The
            # age_category chips above are the group the applicant applied
            # UNDER; birth_year_* is how old they actually ARE. Every one
            # of these is read raw and normalized in the selector, which
            # is lenient about junk by design.
            fee_paid = request.query_params.get("fee_paid")
            birth_year_min = request.query_params.get("birth_year_min")
            birth_year_max = request.query_params.get("birth_year_max")
            age_mismatch = request.query_params.get("age_mismatch")
            # What the PLAYER said about the trial, which is a different
            # question from `status` above: status is the org's decision,
            # this is the hint they are deciding from.
            self_outcome = request.query_params.get("self_outcome")
            sort = request.query_params.get("sort")

            # Validate + clamp pagination — exactly like ListPostLikesAPIView.
            try:
                limit = min(
                    int(request.query_params.get("limit", 20)),
                    50
                )
                offset = max(
                    int(request.query_params.get("offset", 0)),
                    0
                )
            except (ValueError, TypeError):
                return response_data(
                    False,
                    "Invalid pagination params",
                    status_code=400
                )

            # OWNERSHIP GATE — scope the fetch to the actor's org so a missing,
            # soft-deleted, or other-org recruitment all resolve to the same 404
            # (never leak that it exists).
            recruitment = Recruitment.objects.filter(
                id=recruitment_id,
                is_deleted=False,
                organization=actor.organization
            ).first()

            if not recruitment:
                return response_data(
                    success=False,
                    message="Recruitment not found",
                    status_code=status.HTTP_404_NOT_FOUND
                )

            applications, total_count, no_birth_year_count = (
                ApplicationSelector.list_applications(
                    recruitment=recruitment,
                    status=status_filter,
                    search=search,
                    age_category=age_category,
                    fee_paid=fee_paid,
                    birth_year_min=birth_year_min,
                    birth_year_max=birth_year_max,
                    age_mismatch=age_mismatch,
                    self_outcome=self_outcome,
                    sort=sort,
                    limit=limit,
                    offset=offset
                )
            )

            status_counts = ApplicationSelector.status_counts(
                recruitment
            )

            # Highlight counts for the whole page in ONE grouped query — the
            # "▶ Highlights (n)" chip must not cost a query per applicant.
            highlight_counts = visible_highlight_counts_for(
                [app.applicant_id for app in applications],
                request.actor
            )

            serializer = ApplicantListItemSerializer(
                applications,
                many=True,
                context={"highlight_counts": highlight_counts}
            )

            logger.info(
                f"{TAG} | recruitment={recruitment.id} | "
                f"total={total_count} | returned={len(serializer.data)}"
            )

            return response_data(
                success=True,
                data={
                    "count": total_count,
                    "limit": limit,
                    "offset": offset,
                    "results": serializer.data,
                    "status_counts": status_counts,
                    # How many applicants a birth-year range is hiding.
                    # 0 when no range is active. Without it the org
                    # silently loses people from the list and never knows.
                    "no_birth_year_count": no_birth_year_count,
                }
            )

        except Exception as e:
            logger.error(
                f"{TAG} | Error | {str(e)}"
            )
            return response_data(
                success=False,
                message="Something went wrong",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                error=str(e)
            )


class MyApplicationsAPIView(BaseAPIView):

    @player_required
    def get(self, request):
        TAG = "MyApplicationsAPIView"
        try:
            actor = request.actor

            status_filter = request.query_params.get("status")

            # Validate + clamp pagination — same shape as the org-side list.
            try:
                limit = min(
                    int(request.query_params.get("limit", 20)),
                    50
                )
                offset = max(
                    int(request.query_params.get("offset", 0)),
                    0
                )
            except (ValueError, TypeError):
                return response_data(
                    False,
                    "Invalid pagination params",
                    status_code=400
                )

            applications, total_count = (
                ApplicationSelector.list_my_applications(
                    user=actor.user,
                    status=status_filter,
                    limit=limit,
                    offset=offset
                )
            )

            serializer = MyApplicationListSerializer(
                applications,
                many=True
            )

            logger.info(
                f"{TAG} | user={actor.user.id} | "
                f"total={total_count} | returned={len(serializer.data)}"
            )

            return response_data(
                success=True,
                data={
                    "count": total_count,
                    "limit": limit,
                    "offset": offset,
                    "results": serializer.data,
                }
            )

        except Exception as e:
            logger.error(
                f"{TAG} | Error | {str(e)}"
            )
            return response_data(
                success=False,
                message="Something went wrong",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                error=str(e)
            )


class RecruitmentApplicationDetailAPIView(BaseAPIView):

    @org_required
    def get(self, request, application_id):
        TAG = "RecruitmentApplicationDetailAPIView"
        try:
            actor = request.actor

            application = (
                ApplicationSelector.get_application_detail(
                    application_id=application_id
                )
            )

            # OWNERSHIP GATE — the actor's org must own the parent recruitment.
            # Missing application OR another org's application → same 404, so we
            # never leak that the application exists.
            if (
                not application
                or str(application.recruitment.organization_id)
                != str(actor.organization.id)
            ):
                return response_data(
                    success=False,
                    message="Application not found",
                    status_code=status.HTTP_404_NOT_FOUND
                )

            serializer = ApplicationDetailSerializer(application)

            logger.info(
                f"{TAG} | Success | application_id={application.id}"
            )

            return response_data(
                success=True,
                data=serializer.data
            )

        except Exception as e:
            logger.error(
                f"{TAG} | Error | {str(e)}"
            )
            return response_data(
                success=False,
                message="Something went wrong",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                error=str(e)
            )


class WithdrawApplicationAPIView(BaseAPIView):

    @player_required
    def post(self, request, application_id):
        TAG = "WithdrawApplicationAPIView"
        try:
            actor = request.actor

            # OWNERSHIP GATE — missing OR another player's application → the same
            # 404 (never leak that it exists). The service re-checks under lock.
            if not RecruitmentApplication.objects.filter(
                id=application_id,
                applicant=actor.user
            ).exists():
                return response_data(
                    success=False,
                    message="Application not found",
                    status_code=status.HTTP_404_NOT_FOUND
                )

            application = ApplicationService.withdraw(
                actor=actor,
                application_id=application_id
            )

            logger.info(
                f"{TAG} | Withdrawn | application_id={application.id}"
            )

            return response_data(
                success=True,
                message="Application withdrawn",
                data={
                    "application_id": str(application.id),
                    "status": application.status
                }
            )

        except ValidationError as e:
            flat = flatten_validation_error(e.detail)
            logger.warning(
                f"{TAG} | Validation Error | {flat['message']}"
            )
            return response_data(
                success=False,
                message=flat["message"],
                status_code=400,
                error=flat["message"],
                data={"errors": flat["errors"]}
            )

        except Exception as e:
            logger.error(
                f"{TAG} | Error | {str(e)}"
            )
            return response_data(
                success=False,
                message="Something went wrong",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                error=str(e)
            )


class BulkApplicationStatusAPIView(BaseAPIView):

    @org_required
    def post(self, request, recruitment_id):
        TAG = "BulkApplicationStatusAPIView"
        try:
            actor = request.actor

            # OWNERSHIP GATE — scope to the actor's org (missing / other-org →
            # same 404).
            recruitment = Recruitment.objects.filter(
                id=recruitment_id,
                is_deleted=False,
                organization=actor.organization
            ).first()

            if not recruitment:
                return response_data(
                    success=False,
                    message="Recruitment not found",
                    status_code=status.HTTP_404_NOT_FOUND
                )

            serializer = BulkApplicationStatusSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)

            result = ApplicationService.change_status(
                actor=actor,
                recruitment=recruitment,
                application_ids=serializer.validated_data["application_ids"],
                to_status=serializer.validated_data["status"],
                note=serializer.validated_data.get("note", "")
            )

            status_counts = ApplicationSelector.status_counts(recruitment)

            updated_count = len(result["updated"])
            skipped_count = len(result["skipped"])

            logger.info(
                f"{TAG} | recruitment={recruitment.id} | "
                f"updated={updated_count} | skipped={skipped_count} | "
                f"status={serializer.validated_data['status']}"
            )

            return response_data(
                success=True,
                message=f"{updated_count} application(s) updated",
                data={
                    "updated": result["updated"],
                    "skipped": result["skipped"],
                    "status_counts": status_counts
                }
            )

        except ValidationError as e:
            flat = flatten_validation_error(e.detail)
            logger.warning(
                f"{TAG} | Validation Error | {flat['message']}"
            )
            return response_data(
                success=False,
                message=flat["message"],
                status_code=400,
                error=flat["message"],
                data={"errors": flat["errors"]}
            )

        except Exception as e:
            logger.error(
                f"{TAG} | Error | {str(e)}"
            )
            return response_data(
                success=False,
                message="Something went wrong",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                error=str(e)
            )


class ApplicationStatusAPIView(BaseAPIView):

    # Human copy for a single change that the service skipped.
    _SKIP_MESSAGES = {
        "withdrawn": "This applicant withdrew their application.",
        "no_change": "Application is already in that status.",
        "not_found": "Application not found.",
    }

    @org_required
    def post(self, request, application_id):
        TAG = "ApplicationStatusAPIView"
        try:
            actor = request.actor

            # OWNERSHIP GATE — resolve + verify the org owns the parent
            # recruitment (other org / missing → same 404).
            application = (
                RecruitmentApplication.objects
                .select_related("recruitment", "recruitment__organization")
                .filter(id=application_id)
                .first()
            )
            if (
                not application
                or str(application.recruitment.organization_id)
                != str(actor.organization.id)
            ):
                return response_data(
                    success=False,
                    message="Application not found",
                    status_code=status.HTTP_404_NOT_FOUND
                )

            serializer = SingleApplicationStatusSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)

            to_status = serializer.validated_data["status"]

            result = ApplicationService.change_status(
                actor=actor,
                recruitment=application.recruitment,
                application_ids=[application.id],
                to_status=to_status,
                note=serializer.validated_data.get("note", "")
            )

            # A single skipped item is a hard failure for this endpoint → 400.
            if result["skipped"]:
                reason = result["skipped"][0]["reason"]
                message = self._SKIP_MESSAGES.get(
                    reason, "Could not update the application."
                )
                return response_data(
                    success=False,
                    message=message,
                    status_code=400,
                    error=message,
                    data={"errors": {"non_field_errors": message}}
                )

            status_counts = ApplicationSelector.status_counts(
                application.recruitment
            )

            logger.info(
                f"{TAG} | application={application.id} | status={to_status}"
            )

            return response_data(
                success=True,
                message="Status updated",
                data={
                    "application_id": str(application.id),
                    "status": to_status,
                    "status_counts": status_counts
                }
            )

        except ValidationError as e:
            flat = flatten_validation_error(e.detail)
            logger.warning(
                f"{TAG} | Validation Error | {flat['message']}"
            )
            return response_data(
                success=False,
                message=flat["message"],
                status_code=400,
                error=flat["message"],
                data={"errors": flat["errors"]}
            )

        except Exception as e:
            logger.error(
                f"{TAG} | Error | {str(e)}"
            )
            return response_data(
                success=False,
                message="Something went wrong",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                error=str(e)
            )


class ApplicationFeeAPIView(BaseAPIView):
    """
    PATCH /recruitments/applications/<application_id>/fee

    Any org member — it is the person on the gate who knows whether the fee
    was collected, not the admin.
    """

    @org_required
    def patch(self, request, application_id):
        TAG = "ApplicationFeeAPIView"
        try:
            actor = request.actor

            # OWNERSHIP GATE — same shape as ApplicationStatusAPIView: other
            # org / missing resolve to the same 404.
            application = (
                RecruitmentApplication.objects
                .select_related("recruitment", "recruitment__organization")
                .filter(id=application_id)
                .first()
            )
            if (
                not application
                or str(application.recruitment.organization_id)
                != str(actor.organization.id)
            ):
                return response_data(
                    success=False,
                    message="Application not found",
                    status_code=status.HTTP_404_NOT_FOUND
                )

            serializer = ApplicationFeeSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            fee_paid = serializer.validated_data["fee_paid"]

            result = ApplicationService.set_fee_paid(
                actor=actor,
                recruitment=application.recruitment,
                application_ids=[application.id],
                fee_paid=fee_paid,
            )

            # A no-op is not an error here, unlike a status change: pressing
            # "paid" on a row that is already paid is what a second tap on a
            # flaky connection looks like, and the answer the client wants is
            # the state, not a complaint.
            application.refresh_from_db(
                fields=["fee_paid", "fee_paid_at"]
            )

            logger.info(
                f"{TAG} | application={application.id} | "
                f"fee_paid={application.fee_paid} | "
                f"changed={bool(result['updated'])}"
            )

            return response_data(
                success=True,
                message="Fee updated",
                data={
                    "application_id": str(application.id),
                    "fee_paid": application.fee_paid,
                    "fee_paid_at": (
                        application.fee_paid_at.isoformat()
                        if application.fee_paid_at
                        else None
                    ),
                }
            )

        except ValidationError as e:
            flat = flatten_validation_error(e.detail)
            logger.warning(f"{TAG} | Validation Error | {flat['message']}")
            return response_data(
                success=False,
                message=flat["message"],
                status_code=400,
                error=flat["message"],
                data={"errors": flat["errors"]}
            )

        except Exception as e:
            logger.error(f"{TAG} | Error | {str(e)}")
            return response_data(
                success=False,
                message="Something went wrong",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                error=str(e)
            )


class BulkApplicationFeeAPIView(BaseAPIView):
    """
    POST /recruitments/<recruitment_id>/applications/bulk-fee

    The gate's bulk tool. Partial success with a skipped list, mirroring
    bulk-status.
    """

    @org_required
    def post(self, request, recruitment_id):
        TAG = "BulkApplicationFeeAPIView"
        try:
            actor = request.actor

            recruitment = Recruitment.objects.filter(
                id=recruitment_id,
                is_deleted=False,
                organization=actor.organization
            ).first()

            if not recruitment:
                return response_data(
                    success=False,
                    message="Recruitment not found",
                    status_code=status.HTTP_404_NOT_FOUND
                )

            serializer = BulkApplicationFeeSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)

            result = ApplicationService.set_fee_paid(
                actor=actor,
                recruitment=recruitment,
                application_ids=serializer.validated_data["application_ids"],
                fee_paid=serializer.validated_data["fee_paid"],
            )

            updated_count = len(result["updated"])
            skipped_count = len(result["skipped"])

            logger.info(
                f"{TAG} | recruitment={recruitment.id} | "
                f"updated={updated_count} | skipped={skipped_count}"
            )

            return response_data(
                success=True,
                message=f"{updated_count} application(s) updated",
                data={
                    "updated": result["updated"],
                    "skipped": result["skipped"],
                }
            )

        except ValidationError as e:
            flat = flatten_validation_error(e.detail)
            logger.warning(f"{TAG} | Validation Error | {flat['message']}")
            return response_data(
                success=False,
                message=flat["message"],
                status_code=400,
                error=flat["message"],
                data={"errors": flat["errors"]}
            )

        except Exception as e:
            logger.error(f"{TAG} | Error | {str(e)}")
            return response_data(
                success=False,
                message="Something went wrong",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                error=str(e)
            )


class MessageApplicantsAPIView(BaseAPIView):
    """
    POST /recruitments/<recruitment_id>/applications/message

    "Message these players." Any org member — announcing to the whole trial is
    the club's voice and is owner/admin-gated, but messaging the four people
    you just watched play is the job of whoever watched them.

    WRITES ROWS AND RETURNS. Like an announcement, and for the same reason: a
    message to 100 people is 100 conversations, 100 pushes and 100 WebSocket
    fan-outs, which is not work a web request should be doing. The outbox
    drain sends them.

    EACH PLAYER GETS IT PRIVATELY. One delivery, one direct thread each —
    nobody is in a group and nobody sees anybody else.
    """

    @org_required
    def post(self, request, recruitment_id):
        TAG = "MessageApplicantsAPIView"
        try:
            actor = request.actor

            recruitment = Recruitment.objects.filter(
                id=recruitment_id,
                is_deleted=False,
                organization=actor.organization
            ).first()

            if not recruitment:
                return response_data(
                    success=False,
                    message="Recruitment not found",
                    status_code=status.HTTP_404_NOT_FOUND
                )

            serializer = MessageApplicantsSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)

            result = AnnouncementService.create_direct(
                actor=actor,
                recruitment=recruitment,
                application_ids=serializer.validated_data["application_ids"],
                body=serializer.validated_data["body"],
            )

            queued = result["queued"]
            logger.info(
                f"{TAG} | recruitment={recruitment.id} | queued={queued} | "
                f"skipped={len(result['skipped'])}"
            )

            return response_data(
                success=True,
                # "queued", not "sent": nothing has been delivered at this
                # instant and the client should not claim otherwise.
                message=f"Message queued for {queued} player(s)",
                data=result,
            )

        except PermissionDenied as e:
            detail = str(getattr(e, "detail", e))
            return response_data(
                success=False,
                message=detail,
                status_code=status.HTTP_403_FORBIDDEN,
                error=detail,
            )

        except ValidationError as e:
            flat = flatten_validation_error(e.detail)
            logger.warning(f"{TAG} | Validation Error | {flat['message']}")
            return response_data(
                success=False,
                message=flat["message"],
                status_code=400,
                error=flat["message"],
                data={"errors": flat["errors"]}
            )

        except Exception as e:
            logger.error(f"{TAG} | Error | {str(e)}")
            return response_data(
                success=False,
                message="Something went wrong",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                error=str(e)
            )


class TrialPassAPIView(BaseAPIView):
    """
    GET /recruitments/applications/<application_id>/pass

    THE APPLICANT ONLY. A pass names one player, their age group and whether
    they have paid; it is not a public artefact like the posting it belongs
    to. Anybody else gets 404, never 403 — the same rule every other
    ownership gate here follows, because 403 confirms the id is real.
    """

    @player_required
    def get(self, request, application_id):
        TAG = "TrialPassAPIView"
        try:
            actor = request.actor

            application = (
                RecruitmentApplication.objects
                .select_related(
                    "applicant__profile",
                    "age_category",
                    "session__location",
                    "recruitment__organization__profile",
                )
                .prefetch_related(
                    "recruitment__sessions__location",
                    "recruitment__requirements",
                )
                .filter(id=application_id, applicant=actor.user)
                .first()
            )

            if not application:
                return response_data(
                    success=False,
                    message="Application not found",
                    status_code=status.HTTP_404_NOT_FOUND
                )

            # A pass exists once the org has CALLED them to the trial.
            # Anything else gets a clear "not yet" rather than a pass with
            # half its fields missing, which would read as a bug on the day
            # it matters most.
            if (
                application.status
                != RecruitmentApplication.Status.TRIAL_CONFIRMED
            ):
                return response_data(
                    success=False,
                    message=(
                        "No pass yet — the organization hasn't confirmed you "
                        "for this trial."
                    ),
                    status_code=status.HTTP_409_CONFLICT,
                    data={"status": application.status},
                )

            return response_data(
                success=True,
                data=build_trial_pass(application),
            )

        except Exception as e:
            logger.error(f"{TAG} | Error | {str(e)}")
            return response_data(
                success=False,
                message="Something went wrong",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                error=str(e)
            )


class TrialFeedbackAPIView(BaseAPIView):
    """
    POST /recruitments/applications/<application_id>/feedback

    THE APPLICANT ONLY, and only about a trial they were actually called to.

        { "attended": true,
          "outcome": "selected" | "not_selected" | "waiting",
          "rating": 1..5,
          "feedback": "..." }

    A HINT FOR THE ORG, NEVER A DECISION. This endpoint writes five columns
    and returns: no status change, no notification, no email, no history row.
    The org discovers it by opening the list they were going to open anyway.

    TWO REFUSALS, and the difference is deliberate:

      * 404 when the application is not theirs, or their status says they were
        never called to the trial. Same as every ownership gate here — 403
        would confirm the id is real, and a 404 on a status they can see for
        themselves leaks nothing.
      * 400 when the trial has not ended yet. The application IS theirs and
        the question is only premature, so saying so plainly beats a 404 that
        reads as "your application vanished".

    RESUBMITTING is fine and expected: "still waiting to hear" stops being
    true the week the org calls. It updates in place and re-stamps feedback_at.
    """

    throttle_classes = [TrialFeedbackThrottle]

    @player_required
    def post(self, request, application_id):
        TAG = "TrialFeedbackAPIView"
        try:
            actor = request.actor

            application = (
                RecruitmentApplication.objects
                .select_related("recruitment")
                .filter(id=application_id, applicant=actor.user)
                .first()
            )

            # NOT THEIRS, or they were never called to the trial. One 404 for
            # both: somebody who was not shortlisted must not be asked how the
            # trial went — it is a bad question and an unkind one — and the
            # answer must not tell them anything either way.
            if (
                not application
                or not status_allows_feedback(application.status)
            ):
                return response_data(
                    success=False,
                    message="Application not found",
                    status_code=status.HTTP_404_NOT_FOUND
                )

            # TOO EARLY. The calendar-day rule the whole app shares, so a
            # trial that ran this morning becomes reportable at midnight.
            if not trial_is_over(application.recruitment):
                return response_data(
                    success=False,
                    message=(
                        "You can share how it went once the trial has "
                        "finished."
                    ),
                    status_code=status.HTTP_400_BAD_REQUEST,
                    error="trial_not_over",
                )

            serializer = TrialFeedbackSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)

            application = TrialFeedbackService.submit(
                application, serializer.validated_data
            )

            logger.info(
                f"{TAG} | application={application.id} | "
                f"attended={application.attended_self_reported} | "
                f"outcome={application.outcome_self_reported or '-'}"
            )

            return response_data(
                success=True,
                message="Thanks — this helps the organization.",
                data={
                    "attended_self_reported":
                        application.attended_self_reported,
                    "outcome_self_reported":
                        application.outcome_self_reported,
                    "trial_rating": application.trial_rating,
                    "trial_feedback": application.trial_feedback,
                    "feedback_at": application.feedback_at,
                },
            )

        except ValidationError as e:
            flat = flatten_validation_error(e.detail)
            logger.warning(f"{TAG} | Validation Error | {flat['message']}")
            return response_data(
                success=False,
                message=flat["message"],
                status_code=400,
                error=flat["message"],
                data={"errors": flat["errors"]}
            )

        except Exception as e:
            logger.error(f"{TAG} | Error | {str(e)}")
            return response_data(
                success=False,
                message="Something went wrong",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                error=str(e)
            )
