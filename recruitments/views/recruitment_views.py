import logging
from rest_framework import status
from rest_framework.exceptions import ValidationError
from core.views.base_views import BaseAPIView
from recruitments.serializers.recruitment_serializers import (
    RecruitmentCreateSerializer
)
from recruitments.services.recruitment_service import (
    RecruitmentService
)
from utils.response import response_data
from core.decorators.actor_required import org_required
from recruitments.selectors.recruitment_selectors import RecruitmentSelector
from recruitments.serializers.recruitment_list_serializers import (
    RecruitmentListSerializer, RecruitmentOwnerDetailSerializer, RecruitmentDetailSerializer
)


logger = logging.getLogger(__name__)


class CreateRecruitmentAPIView(BaseAPIView):

    @org_required
    def post(self, request):
        TAG = "CreateRecruitmentAPIView"
        try:
            serializer = RecruitmentCreateSerializer(
                data=request.data,
                context={
                    "request": request
                }
            )
            serializer.is_valid(raise_exception=True)
            recruitment = RecruitmentService.create_recruitment(
                actor=request.actor,
                validated_data=serializer.validated_data
            )

            logger.info(
                f"{TAG} | Recruitment created | "
                f"recruitment_id={recruitment.id}"
            )

            return response_data(
                success=True,
                message="Recruitment created successfully",
                data={
                    "recruitment_id": str(recruitment.id)
                }
            )

        except ValidationError as e:
            logger.warning(
                f"{TAG} | Validation Error | {str(e)}"
            )
            return response_data(
                success=False,
                message="Validation error",
                status_code=400,
                error=str(e)
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
        



class ListRecruitmentsAPIView(BaseAPIView):

    def get(self, request):

        TAG = "ListRecruitmentsAPIView"

        try:
            actor = request.actor
            username = request.query_params.get("username")
            sport_id = request.query_params.get("sport_id")
            recruitment_type = request.query_params.get(
                "recruitment_type"
            )
            status_filter = request.query_params.get("status")
            city = request.query_params.get("city")

            limit = min(
                int(request.query_params.get("limit", 10)),
                50
            )

            offset = max(
                int(request.query_params.get("offset", 0)),
                0
            )

            # FETCH DATA
            queryset, total_count = (
                RecruitmentSelector.list_recruitments(
                    actor=actor,
                    username=username,
                    sport_id=sport_id,
                    recruitment_type=recruitment_type,
                    status=status_filter,
                    city=city,
                    limit=limit,
                    offset=offset
                )
            )

            # SERIALIZE
            serializer = RecruitmentListSerializer(
                queryset,
                many=True
            )

            logger.info(
                f"{TAG} | Success | count={len(serializer.data)}"
            )

            return response_data(
                success=True,
                data={
                    "count": total_count,
                    "limit": limit,
                    "offset": offset,
                    "results": serializer.data
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
        



class RecruitmentDetailAPIView(BaseAPIView):

    def get(self, request, recruitment_id):

        TAG = "RecruitmentDetailAPIView"

        print(recruitment_id, '---')

        try:
            actor = request.actor

            # FETCH RECRUITMENT
            recruitment = (
                RecruitmentSelector.get_recruitment_detail(
                    recruitment_id=recruitment_id,
                    actor=actor
                )
            )
            if not recruitment:
                return response_data(
                    success=False,
                    message="Recruitment not found",
                    status_code=status.HTTP_404_NOT_FOUND
                )

            # OWNER CHECK
            is_owner = (
                actor
                and actor.is_org
                and str(actor.organization.id)
                == str(recruitment.organization_id)
            )

            # SERIALIZER
            serializer_class = (
                RecruitmentOwnerDetailSerializer
                if is_owner
                else RecruitmentDetailSerializer
            )

            serializer = serializer_class(
                recruitment,
                context={
                    "request": request
                }
            )

            logger.info(
                f"{TAG} | Success | "
                f"recruitment_id={recruitment.id}"
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