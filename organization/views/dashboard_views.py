import logging

from core.views.base_views import BaseAPIView
from core.decorators.actor_required import org_required
from organization.selectors.dashboard_selectors import DashboardSelector
from utils.response import response_data


logger = logging.getLogger(__name__)


class OrganizationDashboardAPIView(BaseAPIView):
    """
    GET /organizations/dashboard?range=30

    Aggregated overview for the acting organization's admin dashboard. The actor
    must be an organization (membership already verified by resolve_actor);
    @org_required blocks user actors. `range` is one of 7 / 30 / 90 days and
    bounds the in-range stats, the daily trend series and the top-posts window.
    """

    @org_required
    def get(self, request):
        TAG = "OrganizationDashboardAPIView"
        try:
            organization = request.actor.organization

            # Lenient range parsing — junk/unknown values fall back to default
            # rather than erroring the whole dashboard.
            try:
                range_days = int(request.query_params.get("range", DashboardSelector.DEFAULT_RANGE))
            except (ValueError, TypeError):
                range_days = DashboardSelector.DEFAULT_RANGE
            if range_days not in DashboardSelector.ALLOWED_RANGES:
                range_days = DashboardSelector.DEFAULT_RANGE

            data = DashboardSelector.get_dashboard(organization, range_days)

            logger.info(f"{TAG} | org={organization.id} | range={range_days}")

            return response_data(success=True, data=data)
        except Exception as e:
            logger.error(f"{TAG} | Error | {str(e)}")
            return response_data(
                success=False,
                message="Something went wrong",
                status_code=500,
                error=str(e),
            )
