"""
The editor hub and the page's own settings. Thin: read the request, call the
service or selector, shape the answer.

Every route acts as the org through the X-Actor headers. The permission guard
runs BEFORE anything looks at the body — inside each service call, which
checks the permission first — so a coach gets 403, not a critique of a payload
that was never going to be accepted.
"""

from apps.organization.permissions import OrgPermission, has_org_permission
from apps.orgpages.selectors.editor_selectors import page_status, section_status
from apps.orgpages.selectors.readiness_selectors import readiness
from apps.orgpages.serializers.page_serializers import settings_payload
from apps.orgpages.services.guards import page_settings_for, require_page_permission
from apps.orgpages.services.settings_service import PageSettingsService
from apps.orgpages.views.base import PageBaseView, body_of
from utils.response import response_data


class PageAdminAPIView(PageBaseView):
    """
    GET /page/admin — everything the editor hub needs. Readable by any member
    with ``members.view``; ``can_edit`` says whether they may change it.
    """

    def get(self, request):
        organization, member = require_page_permission(
            request.actor, OrgPermission.MEMBERS_VIEW
        )
        page = page_settings_for(organization)
        status = page_status(organization)

        return response_data(
            success=True,
            data={
                "settings": settings_payload(page),
                "can_edit": has_org_permission(member, OrgPermission.PAGE_MANAGE),
                "sections": section_status(page, status),
                "readiness": readiness(organization, page, status),
            },
        )


class PageContactAPIView(PageBaseView):
    """PATCH /page/contact → settings"""

    def patch(self, request):
        page = PageSettingsService.update_contact(request.actor, body_of(request))
        return response_data(
            success=True, message="Contact options saved", data=settings_payload(page)
        )


class PageFactsAPIView(PageBaseView):
    """PATCH /page/facts (partial) → settings"""

    def patch(self, request):
        page = PageSettingsService.update_facts(request.actor, body_of(request))
        return response_data(
            success=True, message="Key facts saved", data=settings_payload(page)
        )


class PageSectionsAPIView(PageBaseView):
    """PATCH /page/sections {key: bool, …} → settings"""

    def patch(self, request):
        page = PageSettingsService.update_sections(request.actor, body_of(request))
        return response_data(
            success=True, message="Sections saved", data=settings_payload(page)
        )
