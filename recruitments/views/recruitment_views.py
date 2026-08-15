import logging
from rest_framework import status
from rest_framework.exceptions import ValidationError
from core.views.base_views import BaseAPIView
from recruitments.serializers.recruitment_serializers import (
    RecruitmentCreateSerializer, RecruitmentUpdateSerializer,
    ChangeRecruitmentStatusSerializer
)
from recruitments.services.recruitment_service import (
    RecruitmentService
)
from utils.response import response_data
from utils.errors import flatten_validation_error
from core.decorators.actor_required import org_required
from recruitments.selectors.recruitment_selectors import RecruitmentSelector
from recruitments.serializers.recruitment_list_serializers import (
    RecruitmentListSerializer, RecruitmentOwnerDetailSerializer,
    RecruitmentDetailSerializer, RecruitmentDiscoverItemSerializer
)
from recruitments.services.discover_service import (
    RecruitmentDiscoverService, SECTION_ORDER
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
        



class UpdateRecruitmentAPIView(BaseAPIView):

    @org_required
    def patch(self, request, recruitment_id):
        TAG = "UpdateRecruitmentAPIView"
        try:
            actor = request.actor

            # FETCH RECRUITMENT (reuses the visibility-aware selector)
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

            # OWNER CHECK (same pattern as RecruitmentDetailAPIView)
            is_owner = (
                actor
                and actor.is_org
                and str(actor.organization.id)
                == str(recruitment.organization_id)
            )
            if not is_owner:
                return response_data(
                    success=False,
                    message=(
                        "You do not have permission "
                        "to edit this recruitment"
                    ),
                    status_code=status.HTTP_403_FORBIDDEN
                )

            serializer = RecruitmentUpdateSerializer(
                data=request.data,
                context={
                    "request": request,
                    "recruitment": recruitment
                }
            )
            serializer.is_valid(raise_exception=True)
            recruitment = RecruitmentService.update_recruitment(
                actor=request.actor,
                recruitment=recruitment,
                validated_data=serializer.validated_data
            )

            logger.info(
                f"{TAG} | Recruitment updated | "
                f"recruitment_id={recruitment.id}"
            )

            return response_data(
                success=True,
                message="Recruitment updated successfully",
                data={
                    "recruitment_id": str(recruitment.id)
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




class ChangeRecruitmentStatusAPIView(BaseAPIView):

    @org_required
    def patch(self, request, recruitment_id):
        TAG = "ChangeRecruitmentStatusAPIView"
        try:
            actor = request.actor

            # FETCH RECRUITMENT (reuses the visibility-aware selector)
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

            # OWNER CHECK (same pattern as UpdateRecruitmentAPIView)
            is_owner = (
                actor
                and actor.is_org
                and str(actor.organization.id)
                == str(recruitment.organization_id)
            )
            if not is_owner:
                return response_data(
                    success=False,
                    message=(
                        "You do not have permission "
                        "to change this recruitment"
                    ),
                    status_code=status.HTTP_403_FORBIDDEN
                )

            serializer = ChangeRecruitmentStatusSerializer(
                data=request.data
            )
            serializer.is_valid(raise_exception=True)
            recruitment = RecruitmentService.change_status(
                actor=request.actor,
                recruitment=recruitment,
                new_status=serializer.validated_data["status"]
            )

            logger.info(
                f"{TAG} | Recruitment status updated | "
                f"recruitment_id={recruitment.id} | "
                f"status={recruitment.status}"
            )

            return response_data(
                success=True,
                message="Recruitment status updated",
                data={
                    "recruitment_id": str(recruitment.id),
                    "status": recruitment.status
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
            search = request.query_params.get("search")
            experience_level = request.query_params.get(
                "experience_level"
            )
            apply_method = request.query_params.get("apply_method")
            position_id = request.query_params.get("position_id")

            # birth_year is a lenient int filter — a non-integer value is simply
            # ignored (dropped to None) instead of 500ing the whole list.
            birth_year = request.query_params.get("birth_year")
            try:
                birth_year = (
                    int(birth_year)
                    if birth_year not in (None, "")
                    else None
                )
            except (ValueError, TypeError):
                birth_year = None

            # max_distance_km — same leniency. None means "no distance filter",
            # which is NOT the same as discover's 50 km default.
            max_distance_km = request.query_params.get("max_distance_km")
            try:
                max_distance_km = (
                    int(max_distance_km)
                    if max_distance_km not in (None, "")
                    else None
                )
            except (ValueError, TypeError):
                max_distance_km = None
            if max_distance_km is not None and max_distance_km <= 0:
                max_distance_km = None

            age_eligible = (
                request.query_params.get("age_eligible") in ("1", "true", "True")
            )

            # The two §5 rail deep-links ("Closing soon" / "New this week"),
            # expressed as filters so "See all" lands on the same rule the rail
            # was built from. Lenient ints, same as the rest.
            def _positive_int(name):
                raw = request.query_params.get(name)
                try:
                    value = int(raw) if raw not in (None, "") else None
                except (ValueError, TypeError):
                    return None
                return value if value and value > 0 else None

            closing_within_days = _positive_int("closing_within_days")
            published_within_days = _positive_int("published_within_days")

            limit = min(
                int(request.query_params.get("limit", 10)),
                50
            )

            offset = max(
                int(request.query_params.get("offset", 0)),
                0
            )

            filters = dict(
                username=username,
                sport_id=sport_id,
                recruitment_type=recruitment_type,
                status=status_filter,
                city=city,
                search=search,
                experience_level=experience_level,
                apply_method=apply_method,
                birth_year=birth_year,
                position_id=position_id,
                max_distance_km=max_distance_km,
                closing_within_days=closing_within_days,
                published_within_days=published_within_days,
            )

            # ORDERING (§4). A player browsing the global list gets
            # -match_score; every org-scoped mount — the org admin screen and
            # the public org profile, both of which pass ``username`` — keeps
            # -published_at, unchanged. An org actor has no match score worth
            # sorting by either way.
            ranked = bool(actor and actor.is_user and not username)

            if ranked:
                rows, total_count = RecruitmentDiscoverService.ranked_list(
                    actor=actor,
                    filters=filters,
                    age_eligible=age_eligible,
                    limit=limit,
                    offset=offset,
                )
                results = []
                for recruitment, match in rows:
                    recruitment.match = match
                    results.append(recruitment)

                serializer = RecruitmentDiscoverItemSerializer(
                    results,
                    many=True
                )
            else:
                queryset, total_count = (
                    RecruitmentSelector.list_recruitments(
                        actor=actor,
                        limit=limit,
                        offset=offset,
                        **filters
                    )
                )
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
        



class DiscoverRecruitmentsAPIView(BaseAPIView):
    """
    GET /recruitments/discover — the personalized surface (§4).

    Four sections, each capped at 10 and deduplicated in priority order, cached
    per actor for 10 minutes. Never errors on a thin profile: an org actor or a
    player with no sports gets the same shape with ``is_personalized: false``,
    and the client shows the profile-completion prompt instead of an error.
    """

    def get(self, request):

        TAG = "DiscoverRecruitmentsAPIView"

        try:
            actor = request.actor

            max_distance_km = RecruitmentDiscoverService.normalize_max_distance(
                request.query_params.get("max_distance_km")
            )

            cache_key = RecruitmentDiscoverService.cache_key(
                actor, max_distance_km
            )
            cached = RecruitmentDiscoverService.get_cached(cache_key)
            if cached is not None:
                logger.info(f"{TAG} | Cache hit")
                return response_data(success=True, data=cached)

            payload = RecruitmentDiscoverService.discover(
                actor=actor,
                max_distance_km=max_distance_km,
            )

            data = {"max_distance_km": payload.max_distance_km}
            for section in SECTION_ORDER:
                items = []
                for recruitment, match in payload.sections[section]:
                    recruitment.match = match
                    items.append(recruitment)
                data[section] = RecruitmentDiscoverItemSerializer(
                    items, many=True
                ).data

            # Why the payload says so rather than the client inferring it: the
            # client cannot tell "no sports on file" from "no matching trials".
            data["is_personalized"] = payload.is_personalized
            data["missing_profile_fields"] = payload.missing_fields

            RecruitmentDiscoverService.set_cached(cache_key, data)

            # §8 — logged on cache MISS only; the cache window is the serve
            # window. See record_impressions.
            RecruitmentDiscoverService.record_impressions(
                actor, payload.sections
            )

            logger.info(
                f"{TAG} | Success | "
                + " ".join(
                    f"{section}={len(data[section])}"
                    for section in SECTION_ORDER
                )
            )

            return response_data(success=True, data=data)

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