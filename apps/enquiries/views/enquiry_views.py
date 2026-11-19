"""
Enquiries over HTTP. Thin: read the request, call the service or selector,
shape the answer. Every refusal is ``{code, message}`` in ``data`` with field
errors in ``errors[field]`` (the page's error mixin).

  * The form — public, no account needed, NOT under /public/ (that prefix is
    the allow-list of anonymous READS; this is a write).
  * The inbox — X-Actor: organization, ``enquiries.manage``.
  * The email switch — the person's own, as themselves.
"""

from rest_framework.exceptions import ParseError

from apps.enquiries.selectors.inbox_selectors import (
    inbox_page,
    org_enquiry,
    unread_count,
)
from apps.enquiries.errors import enquiry_not_found
from apps.enquiries.serializers import enquiry_detail, inbox_item
from apps.enquiries.services.inbox_service import (
    EnquiryAlertPreferenceService,
    EnquiryInboxService,
    require_inbox,
)
from apps.enquiries.services.submit_service import EnquirySubmitService
from apps.enquiries.throttles import EnquirySubmitThrottle
from apps.orgpages.views.base import PageBaseView, PageErrorsMixin, body_of
from core.views.base_views import PublicAPIView
from utils.response import response_data


class EnquirySubmitAPIView(PageErrorsMixin, PublicAPIView):
    """
    POST /enquiries/submit/<org_username>
    → 201 {ok: true, enquiry: {program_name, location_name, phone_masked}}

    PublicAPIView's default is the 60/min public READ budget; this write has
    its own, 30/hour per IP for every caller.
    """

    throttle_classes = [EnquirySubmitThrottle]

    def post(self, request, org_username):
        try:
            payload = request.data
        except ParseError:
            payload = {}

        user = request.user if request.user.is_authenticated else None
        answer = EnquirySubmitService.submit(
            org_username,
            payload if isinstance(payload, dict) else {},
            user=user,
            actor=request.actor,
        )
        return response_data(
            success=True,
            message="Enquiry sent",
            data={"ok": True, "enquiry": answer},
            status_code=201,
        )


class EnquiryInboxAPIView(PageBaseView):
    """GET /enquiries/inbox ?status= &program_id= &location_id= &cursor="""

    def get(self, request):
        organization, _ = require_inbox(request.actor)
        page = inbox_page(organization, request.query_params)
        return response_data(
            success=True,
            data={
                "items": [inbox_item(enquiry) for enquiry in page["items"]],
                "next_cursor": page["next_cursor"],
                "counts": page["counts"],
            },
        )


class EnquiryUnreadCountAPIView(PageBaseView):
    """GET /enquiries/inbox/unread-count → {new: n}"""

    def get(self, request):
        organization, _ = require_inbox(request.actor)
        return response_data(success=True, data={"new": unread_count(organization)})


class EnquiryDetailAPIView(PageBaseView):
    """GET /enquiries/<uuid> · PATCH /enquiries/<uuid> {status?, notes?}"""

    def get(self, request, enquiry_id):
        organization, _ = require_inbox(request.actor)
        enquiry = org_enquiry(organization, enquiry_id)
        if enquiry is None:
            raise enquiry_not_found()
        return response_data(success=True, data=enquiry_detail(enquiry))

    def patch(self, request, enquiry_id):
        EnquiryInboxService.update(request.actor, enquiry_id, body_of(request))
        enquiry = org_enquiry(request.actor.organization, enquiry_id)
        return response_data(
            success=True, message="Enquiry saved", data=enquiry_detail(enquiry)
        )


class EnquiryAlertsAPIView(PageBaseView):
    """
    GET /enquiries/alerts?org_id= → {email}
    PATCH /enquiries/alerts {org_id, email} → {email}

    The person's own switch for enquiry emails from one org. As themselves
    (user actor), and only for an org where they hold enquiries.manage —
    anything else is 404.
    """

    def get(self, request):
        email = EnquiryAlertPreferenceService.get(
            request.actor, request.query_params.get("org_id")
        )
        return response_data(success=True, data={"email": email})

    def patch(self, request):
        email = EnquiryAlertPreferenceService.set(request.actor, body_of(request))
        return response_data(success=True, message="Saved", data={"email": email})
