import logging
from django.db import transaction
from rest_framework import status

from core.views.base_views import BaseAPIView
from recruitments.models import Recruitment
from recruitments.selectors.recruitment_selectors import (
    LIST_PREFETCH_RELATED, LIST_SELECT_RELATED, RecruitmentSelector
)
from recruitments.selectors.saved_recruitment_selectors import (
    SavedRecruitmentSelector
)
from recruitments.serializers.recruitment_list_serializers import (
    SavedRecruitmentListSerializer
)
from recruitments.services.saved_recruitment_service import (
    SavedRecruitmentService
)
from recruitments.throttles import SaveRecruitmentThrottle
from utils.response import response_data

logger = logging.getLogger(__name__)


class ToggleSaveRecruitmentAPIView(BaseAPIView):
    """
    POST /recruitments/<uuid:recruitment_id>/save

    Shortlists for the CURRENT actor — the signed-in person, or the org when
    acting through the X-Actor-* headers. Saves are private to the saver:
    nothing is counted, notified, or shown to the recruiting org.

    Any authenticated actor may save; the model's check constraint is what
    scopes ownership, exactly as it does for saved posts. What IS gated is
    which recruitments can be reached at all — see the 404 below.
    """

    throttle_classes = [SaveRecruitmentThrottle]

    def post(self, request, recruitment_id):
        TAG = "ToggleSaveRecruitmentAPIView"

        try:
            actor = request.actor

            if not actor or (not actor.is_user and not actor.is_org):
                return response_data(
                    success=False,
                    message="Invalid actor",
                    status_code=400
                )

            with transaction.atomic():
                # Resolved through the DETAIL selector, not a bare pk lookup:
                # saving must not become a way to probe for deleted, draft,
                # private or followers-only postings the caller could not open.
                # Same rules, same 404, one implementation.
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

                is_saved = SavedRecruitmentService.toggle(
                    actor, recruitment
                )

            logger.info(
                f"{TAG} | Success | "
                f"recruitment_id={recruitment.id} | saved={is_saved}"
            )

            return response_data(
                success=True,
                message="Success",
                data={
                    "recruitment_id": str(recruitment.id),
                    "is_saved": is_saved
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


class SavedRecruitmentsListAPIView(BaseAPIView):
    """
    GET /recruitments/saved/list?limit=&offset=

    The CURRENT actor's shortlist, most recently saved first, in the same
    limit/offset envelope as /recruitments/list — and carrying the same card
    payload, so RecruitmentCard renders it unchanged. Plus ``saved_at``, which
    is the one fact only this list knows.

    ``is_saved`` ships here too. Trivially true, but an absent flag would
    render an empty bookmark on the very list the bookmark filled.
    """

    def get(self, request):

        TAG = "SavedRecruitmentsListAPIView"

        try:
            actor = request.actor

            if not actor or (not actor.is_user and not actor.is_org):
                return response_data(
                    success=False,
                    message="Invalid actor",
                    status_code=400
                )

            limit = min(
                int(request.query_params.get("limit", 10)),
                50
            )

            offset = max(
                int(request.query_params.get("offset", 0)),
                0
            )

            # Paginate the SAVE rows, not the recruitments: the ordering is
            # "when I saved it", and that lives on the save. Deleted and
            # no-longer-visible postings are already gone from this queryset
            # (see the selector), so the count matches what the client can
            # actually page through.
            saved_rows = SavedRecruitmentSelector.saved_rows(actor)

            total_count = saved_rows.count()

            page = list(saved_rows[offset: offset + limit])
            page_ids = [row.recruitment_id for row in page]
            saved_at = {row.recruitment_id: row.created_at for row in page}

            recruitments = SavedRecruitmentSelector.annotate_is_saved(
                Recruitment.objects.filter(id__in=page_ids), actor
            ).select_related(
                *LIST_SELECT_RELATED
            ).prefetch_related(
                *LIST_PREFETCH_RELATED
            )

            # ``id__in`` returns them in whatever order the DB likes — restore
            # the saved-at ordering from the page, and stamp on the timestamp
            # the card cannot read off the recruitment itself.
            by_id = {
                recruitment.id: recruitment
                for recruitment in recruitments
            }

            results = []
            for recruitment_id in page_ids:
                recruitment = by_id.get(recruitment_id)
                if not recruitment:
                    continue
                recruitment.saved_at = saved_at[recruitment_id]
                results.append(recruitment)

            serializer = SavedRecruitmentListSerializer(
                results,
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
