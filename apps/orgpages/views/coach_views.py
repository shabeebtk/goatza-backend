"""Coaches on the page, as the club edits them. ``page.manage``."""

from apps.orgpages.selectors.editor_selectors import editor_coaches
from apps.orgpages.serializers.page_serializers import (
    coach_candidate_payload,
    coach_editor_payload,
)
from apps.orgpages.services.coach_service import CoachService, get_org_coach
from apps.orgpages.services.guards import require_page_permission
from apps.orgpages.views.base import PageBaseView, body_of
from utils.response import response_data


def coaches_payload(organization):
    coaches, candidates = editor_coaches(organization)
    return {
        "coaches": [coach_editor_payload(coach) for coach in coaches],
        "candidates": [coach_candidate_payload(member) for member in candidates],
    }


def coach_response(organization, coach_id, *, message="", status_code=200):
    coach = get_org_coach(organization, coach_id)
    return response_data(
        success=True,
        message=message,
        data=coach_editor_payload(coach),
        status_code=status_code,
    )


class PageCoachesAPIView(PageBaseView):
    """GET /page/coaches → {coaches, candidates} · POST /page/coaches → 201 Coach"""

    def get(self, request):
        organization, _ = require_page_permission(request.actor)
        return response_data(success=True, data=coaches_payload(organization))

    def post(self, request):
        coach = CoachService.create(request.actor, body_of(request))
        return coach_response(
            request.actor.organization, coach.id, message="Coach added", status_code=201
        )


class PageCoachDetailAPIView(PageBaseView):
    """PATCH /page/coaches/<uuid> (partial, incl. is_visible) → Coach"""

    def patch(self, request, coach_id):
        CoachService.update(request.actor, coach_id, body_of(request))
        return coach_response(request.actor.organization, coach_id, message="Coach saved")


class PageCoachDeleteAPIView(PageBaseView):
    """POST /page/coaches/<uuid>/delete"""

    def post(self, request, coach_id):
        CoachService.delete(request.actor, coach_id)
        return response_data(success=True, message="Coach removed")


class PageCoachReorderAPIView(PageBaseView):
    """POST /page/coaches/reorder {ids} → {coaches, candidates}"""

    def post(self, request):
        CoachService.reorder(request.actor, body_of(request))
        return response_data(
            success=True,
            message="Order saved",
            data=coaches_payload(request.actor.organization),
        )
