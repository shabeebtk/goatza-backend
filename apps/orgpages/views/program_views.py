"""Programs & fees, as the club edits them. ``page.manage``."""

from apps.orgpages.selectors.editor_selectors import org_programs
from apps.orgpages.serializers.page_serializers import program_payload
from apps.orgpages.services.guards import require_page_permission
from apps.orgpages.services.program_service import ProgramService, get_org_program
from apps.orgpages.views.base import PageBaseView, body_of
from utils.response import response_data


def programs_response(organization, *, message=""):
    return response_data(
        success=True,
        message=message,
        data={"programs": [program_payload(p) for p in org_programs(organization)]},
    )


class PageProgramsAPIView(PageBaseView):
    """GET /page/programs → {programs} · POST /page/programs → 201 Program"""

    def get(self, request):
        organization, _ = require_page_permission(request.actor)
        return programs_response(organization)

    def post(self, request):
        program = ProgramService.create(request.actor, body_of(request))
        program = get_org_program(request.actor.organization, program.id)
        return response_data(
            success=True,
            message="Program added",
            data=program_payload(program),
            status_code=201,
        )


class PageProgramDetailAPIView(PageBaseView):
    """PATCH /page/programs/<uuid> (partial) → Program"""

    def patch(self, request, program_id):
        ProgramService.update(request.actor, program_id, body_of(request))
        program = get_org_program(request.actor.organization, program_id)
        return response_data(
            success=True, message="Program saved", data=program_payload(program)
        )


class PageProgramDeleteAPIView(PageBaseView):
    """POST /page/programs/<uuid>/delete"""

    def post(self, request, program_id):
        ProgramService.delete(request.actor, program_id)
        return response_data(success=True, message="Program deleted")


class PageProgramReorderAPIView(PageBaseView):
    """POST /page/programs/reorder {ids} → {programs}"""

    def post(self, request):
        ProgramService.reorder(request.actor, body_of(request))
        return programs_response(request.actor.organization, message="Order saved")
