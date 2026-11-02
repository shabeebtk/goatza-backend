"""
The author's view of posts and highlights the server is holding while their
media converts, under /media/publications.

Thin: a selector for the list, PublicationService for the two writes, the
upload endpoints' ``upload_error`` for refusals. Always the CURRENT actor's
own — another actor's publication is a 404, never a 403.
"""

import logging

from apps.accounts.views.user_upload_signature_views import upload_error
from apps.media.models import PendingPublication
from apps.media.selectors.publication_selectors import publications_for
from apps.media.serializers.publication_serializers import (
    PendingPublicationSerializer,
)
from apps.media.services.publication_service import (
    PublicationError,
    PublicationService,
)
from core.views.base_views import BaseAPIView
from utils.response import response_data

logger = logging.getLogger(__name__)

# What a client restoring its "Posting…" bar asks for when it names nothing.
DEFAULT_LIST_STATUSES = (
    PendingPublication.Status.PROCESSING,
    PendingPublication.Status.FAILED,
)


def _publication_response(publication):
    return response_data(
        success=True,
        data={"publication": PendingPublicationSerializer(publication).data},
    )


class PublicationListAPIView(BaseAPIView):
    """
    GET /media/publications?status=processing,failed

        {"publications": [{...}]}

    The current actor's own, not dismissed, from the last 7 days, newest
    first, at most 20 — how the client restores its "Posting…" bar after a
    reload. Unknown status values are ignored; none left means the default.
    """

    def get(self, request):
        requested = [
            value.strip()
            for value in (request.query_params.get("status") or "").split(",")
            if value.strip() in PendingPublication.Status.values
        ]
        statuses = requested or list(DEFAULT_LIST_STATUSES)

        publications = publications_for(self.actor, statuses)

        return response_data(
            success=True,
            data={
                "publications": [
                    PendingPublicationSerializer(publication).data
                    for publication in publications
                ]
            },
        )


class PublicationCancelAPIView(BaseAPIView):
    """
    POST /media/publications/<uuid>/cancel

    200 {"publication"} — cancelled (again is fine). 400 code
    ``publication_not_processing`` once it has published or failed. 404 for
    anyone but the owner.
    """

    def post(self, request, publication_id):
        try:
            publication = PublicationService.cancel(self.actor, publication_id)
        except PublicationError as e:
            return upload_error(
                e.message, status_code=e.status_code, code=e.code
            )
        return _publication_response(publication)


class PublicationDismissAPIView(BaseAPIView):
    """
    POST /media/publications/<uuid>/dismiss

    200 {"publication"} — a FAILED one stops coming back from the list. 400
    code ``publication_not_failed`` for anything else. 404 for anyone but the
    owner.
    """

    def post(self, request, publication_id):
        try:
            publication = PublicationService.dismiss(self.actor, publication_id)
        except PublicationError as e:
            return upload_error(
                e.message, status_code=e.status_code, code=e.code
            )
        return _publication_response(publication)
