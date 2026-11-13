"""
HTTP entry points for the organization side of career verification, mounted
under /careers/.

Thin by design, same as career_views: the org/role rule and the "must be
pending" rule both live in CareerVerificationService and reach the client
through ``_service_error``.
"""

import logging

from django.db import transaction
from rest_framework.exceptions import (
    NotFound,
    PermissionDenied,
    ValidationError,
)

from apps.careers.models import CareerEntry
from apps.careers.selectors.career_selectors import (
    career_entries_for,
    decided_verification_requests_for,
    pending_verification_requests_for,
)
from apps.organization.permissions import (
    DENIED_MESSAGES,
    OrgPermission,
    has_org_permission,
)
from apps.squads.errors import SquadError
from apps.squads.selectors.squad_selectors import (
    members_for_response,
    primary_positions_for,
)
from apps.squads.serializers.squad_serializers import MemberItemSerializer
from apps.squads.services.guards import get_org_squad
from apps.squads.services.membership_service import MembershipService
from apps.squads.views.base import squad_error_response
from apps.careers.serializers.career_serializers import (
    CareerEntrySerializer,
    CareerRejectSerializer,
    CareerVerificationRequestSerializer,
)
from apps.careers.services.career_verification_services import CareerVerificationService
from apps.notifications.services.notification_service import NotificationService
from apps.careers.views.career_views import _service_error
from core.views.base_views import BaseAPIView
from utils.errors import error_body
from utils.response import response_data

logger = logging.getLogger(__name__)


class CareerVerificationRequestListAPIView(BaseAPIView):
    """
    GET /careers/verification-requests — the acting org's review screen.

    ``?status=pending`` (default) is the work queue; ``?status=decided`` is the
    history of calls already made, which a club may revisit. Paginated with
    limit/offset like the recruitment applicant list, so a club with a long
    history doesn't ship the whole thing on every open.
    """

    DEFAULT_LIMIT = 20
    MAX_LIMIT = 50

    def get(self, request):
        TAG = "CareerVerificationRequestListAPIView"
        try:
            organization, _ = CareerVerificationService.require_reviewer(
                request.actor
            )

            status = request.query_params.get("status", "pending")
            if status not in ("pending", "decided"):
                return response_data(
                    success=False,
                    message="status must be 'pending' or 'decided'.",
                    status_code=400,
                    data=error_body("status must be 'pending' or 'decided'."),
                )

            limit, offset = self._page(request)

            queryset = (
                decided_verification_requests_for(organization)
                if status == "decided"
                else pending_verification_requests_for(organization)
            )

            # One COUNT for the tab badge, then one page. The count is taken
            # before slicing so "showing 20 of 63" is honest.
            total = queryset.count()
            entries = list(queryset[offset:offset + limit])

            return response_data(
                success=True,
                data={
                    "count": total,
                    "limit": limit,
                    "offset": offset,
                    "has_more": offset + len(entries) < total,
                    "status": status,
                    "results": CareerVerificationRequestSerializer(
                        entries,
                        many=True
                    ).data,
                },
            )

        except (ValidationError, PermissionDenied, NotFound) as e:
            return _service_error(TAG, e)

        except Exception as e:
            logger.error(f"{TAG} | Error | {str(e)}")
            return response_data(
                success=False,
                message="Something went wrong",
                status_code=500,
                error=str(e)
            )

    def _page(self, request):
        """Clamp limit/offset so a client can't ask for the whole table."""
        def as_int(name, default):
            try:
                return int(request.query_params.get(name, default))
            except (TypeError, ValueError):
                return default

        limit = as_int("limit", self.DEFAULT_LIMIT)
        offset = as_int("offset", 0)

        limit = max(1, min(limit, self.MAX_LIMIT))
        offset = max(0, offset)

        return limit, offset


class VerifyCareerEntryAPIView(BaseAPIView):
    """
    POST /careers/<entry_id>/verify — verifications.review confirms the claim.

    Optional body ``{squad_id, position_id?, jersey_number?}``: "Verify and add
    to squad". Without ``squad_id`` nothing about this endpoint changes. With
    it, the actor also needs squads.review, the squad must be this org's, live,
    and for the entry's sport — and the verify and the squad place happen in
    ONE transaction: a squad refusal rolls the verify back too, and answers in
    the squads ``{code, message}`` shape. The response gains ``squad_member``
    (a MemberItem), and the player is told ONCE, as a squad place
    (squad_request_approved), not with career_verified as well.
    """

    def post(self, request, entry_id):
        TAG = "VerifyCareerEntryAPIView"
        body = request.data if isinstance(request.data, dict) else {}

        try:
            if body.get("squad_id"):
                return self._verify_into_squad(request, entry_id, body)

            entry = CareerVerificationService.verify_entry(
                request.actor,
                entry_id
            )

            logger.info(f"{TAG} | Career entry verified | entry_id={entry.id}")

            return response_data(
                success=True,
                message="Career entry verified",
                data=CareerEntrySerializer(entry).data,
            )

        except SquadError as e:
            return squad_error_response(e)

        except (ValidationError, PermissionDenied, NotFound) as e:
            return _service_error(TAG, e)

        except Exception as e:
            logger.error(f"{TAG} | Error | {str(e)}")
            return response_data(
                success=False,
                message="Something went wrong",
                status_code=500,
                error=str(e)
            )

    def _verify_into_squad(self, request, entry_id, body):
        # The career gate first, in its own shape, exactly as a plain verify.
        organization, member = CareerVerificationService.require_reviewer(
            request.actor
        )
        if not has_org_permission(member, OrgPermission.SQUADS_REVIEW):
            raise SquadError(
                "forbidden",
                DENIED_MESSAGES[OrgPermission.SQUADS_REVIEW.value],
                status_code=403,
            )

        with transaction.atomic():
            squad = get_org_squad(organization, body.get("squad_id"))

            # The status before this verify, so "added by mistake" can put it
            # back. Read under the entry's lock; an entry that is missing or
            # another club's is the verify's own refusal below.
            before = (
                CareerEntry.objects.select_for_update()
                .filter(id=entry_id, organization=organization)
                .values("verification_status", "sport_id")
                .first()
            )
            if before is not None and before["sport_id"] != squad.sport_id:
                raise SquadError(
                    "sport_mismatch",
                    "This career entry is for a different sport than the squad.",
                )

            # One notification, as a squad place — not career_verified as well.
            entry = CareerVerificationService.verify_entry(
                request.actor, entry_id, notify=False
            )

            row = MembershipService.add_verified_entry_to_squad(
                member,
                squad,
                entry,
                before["verification_status"] if before else "",
                position_id=body.get("position_id"),
                jersey_number=body.get("jersey_number"),
            )

            # After commit and robust, like every squads side effect: a failed
            # push must not fail or roll back what the club just did.
            def notify_player():
                NotificationService.squad_request_approved(row)

            transaction.on_commit(notify_player, robust=True)

        logger.info(
            "VerifyCareerEntryAPIView | Career entry verified into squad "
            f"| entry_id={entry.id} | squad_id={squad.id} | member_id={row.id}"
        )

        rows = members_for_response([row.id])
        data = CareerEntrySerializer(
            career_entries_for(entry.user).filter(id=entry.id).first() or entry
        ).data
        data["squad_member"] = MemberItemSerializer(
            rows[0],
            context={
                "can_review": True,
                "primary_positions": primary_positions_for(
                    [rows[0].user_id], squad.sport_id
                ),
            },
        ).data

        return response_data(
            success=True,
            message="Career entry verified and added to the squad",
            data=data,
        )


class RejectCareerEntryAPIView(BaseAPIView):
    """POST /careers/<entry_id>/reject — verifications.review declines, with a note."""

    def post(self, request, entry_id):
        TAG = "RejectCareerEntryAPIView"
        try:
            # 403 before 400: a COACH never gets told what was wrong with a body
            # they were not allowed to send.
            CareerVerificationService.require_reviewer(request.actor)

            serializer = CareerRejectSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)

            entry = CareerVerificationService.reject_entry(
                request.actor,
                entry_id,
                reason=serializer.validated_data.get("reason", "")
            )

            logger.info(f"{TAG} | Career entry rejected | entry_id={entry.id}")

            return response_data(
                success=True,
                message="Career entry rejected",
                data=CareerEntrySerializer(entry).data,
            )

        except (ValidationError, PermissionDenied, NotFound) as e:
            return _service_error(TAG, e)

        except Exception as e:
            logger.error(f"{TAG} | Error | {str(e)}")
            return response_data(
                success=False,
                message="Something went wrong",
                status_code=500,
                error=str(e)
            )
