import logging
from rest_framework import status
from rest_framework.exceptions import ValidationError
from core.views.base_views import BaseAPIView
from core.decorators.actor_required import player_required, org_required
from recruitments.models import Recruitment, RecruitmentApplication
from recruitments.selectors.recruitment_selectors import RecruitmentSelector
from recruitments.selectors.application_selectors import ApplicationSelector
from recruitments.serializers.application_serializers import (
    RecruitmentApplySerializer,
    ApplicantListItemSerializer,
    ApplicationDetailSerializer,
    BulkApplicationStatusSerializer,
    SingleApplicationStatusSerializer,
    MyApplicationListSerializer
)
from recruitments.services.application_service import ApplicationService
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

            status_filter = request.query_params.get("status")
            search = request.query_params.get("search", "").strip()

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

            applications, total_count = (
                ApplicationSelector.list_applications(
                    recruitment=recruitment,
                    status=status_filter,
                    search=search,
                    limit=limit,
                    offset=offset
                )
            )

            status_counts = ApplicationSelector.status_counts(
                recruitment
            )

            serializer = ApplicantListItemSerializer(
                applications,
                many=True
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
