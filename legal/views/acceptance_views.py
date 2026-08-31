import logging

from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.views import APIView

from legal.selectors.acceptance_selectors import (
    current_versions,
    get_pending_documents,
)
from legal.services.acceptance_service import record_acceptance
from legal.throttles import LegalAcceptThrottle
from utils.request_meta import client_ip, client_user_agent
from utils.response import response_data

logger = logging.getLogger(__name__)


class LegalVersionsAPIView(APIView):
    """
    GET legal/versions — what every document is currently at.

    AllowAny, and deliberately not under ``public/``: core.public_urls is an
    allow-list of anonymous reads of USER data, and this reads none. It returns
    four constants that are also printed on the documents themselves, so there
    is nothing here to leak.

    Open because the caller who most needs it has no token yet. The signup form
    shows "I accept the terms" before an account exists, and a future mobile
    client checks versions on launch, before restoring a session.
    """

    permission_classes = [AllowAny]

    def get(self, request):
        return response_data(True, data=current_versions())


class AcceptLegalDocumentsAPIView(APIView):
    """
    POST legal/accept — record that the signed-in user accepts the current
    version of the documents they name.

    Body: ``{"documents": ["terms", "privacy"]}``. The VERSION is never in the
    payload; the registry decides it. A client that could name a version could
    accept one that was never published, or re-accept a superseded one and put
    itself back behind the gate.

    This is the version-bump path, not the signup path. At signup there is no
    token yet, so UserSignupAPIView records consent server-side at the moment
    the account row exists — see its ``accepted_terms`` handling.
    """

    permission_classes = [IsAuthenticated]
    throttle_classes = [LegalAcceptThrottle]

    def post(self, request):
        documents = request.data.get("documents")

        # Shape only. WHICH keys are legal is the service's call, and asking
        # twice is how the two answers drift apart.
        if not isinstance(documents, list) or not documents:
            return response_data(
                False,
                message="documents must be a non-empty list",
                status_code=400,
            )

        try:
            acceptances = record_acceptance(
                user=request.user,
                documents=documents,
                ip_address=client_ip(request),
                user_agent=client_user_agent(request),
            )
        except ValueError as ve:
            # An unknown document key, and nothing was written — the service
            # validates the whole list before it opens the transaction.
            logger.warning(
                f"[LEGAL ACCEPT] Rejected | user={request.user.id} | {str(ve)}"
            )
            return response_data(False, message=str(ve), status_code=400)

        # Re-read from the same user instance the service just updated, so the
        # pending list in this response reflects the write that produced it.
        return response_data(
            True,
            message="Acceptance recorded",
            data={
                "accepted": [row.document for row in acceptances],
                "pending": get_pending_documents(request.user),
            },
        )
