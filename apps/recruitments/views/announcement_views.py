# recruitments/views/announcement_views.py
"""
The announcement endpoints.

NOTHING HERE SENDS ANYTHING. The create endpoint writes the announcement and
its outbox rows and returns; ``manage.py dispatch_announcements`` does the
sending, off the request. See AnnouncementDelivery's model docstring for why
that split is not optional in this codebase.

Visibility on the read side is the SELECTOR's job
(``AnnouncementSelector.list_for_actor``), the same way recruitment visibility
lives in ``RecruitmentSelector`` — one home per rule.
"""

import logging

from rest_framework import status
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.recruitments.models import Recruitment, RecruitmentAnnouncement
from apps.recruitments.selectors.announcement_selectors import (
    AnnouncementSelector,
)
from apps.recruitments.selectors.recruitment_selectors import RecruitmentSelector
from apps.recruitments.serializers.announcement_serializers import (
    DELETE_MESSAGE,
    AnnouncementCreateSerializer,
    AnnouncementSerializer,
    RecipientsCountQuerySerializer,
)
from apps.recruitments.services.announcement_service import AnnouncementService
from core.decorators.actor_required import org_required
from core.views.base_views import BaseAPIView
from utils.errors import flatten_validation_error
from utils.response import response_data

logger = logging.getLogger(__name__)


def _forbidden(exc):
    detail = str(getattr(exc, "detail", exc))
    return response_data(
        success=False,
        message=detail,
        status_code=status.HTTP_403_FORBIDDEN,
        error=detail,
    )


def _invalid(tag, exc):
    flat = flatten_validation_error(exc.detail)
    logger.warning(f"{tag} | Validation Error | {flat['message']}")
    return response_data(
        success=False,
        message=flat["message"],
        status_code=400,
        error=flat["message"],
        data={"errors": flat["errors"]},
    )


def _owned_recruitment(actor, recruitment_id):
    """
    The org's own posting, or None. Missing, soft-deleted and another org's
    all resolve to the same None so a prober learns nothing.
    """
    return Recruitment.objects.filter(
        id=recruitment_id,
        is_deleted=False,
        organization=actor.organization,
    ).first()


class RecruitmentAnnouncementsAPIView(BaseAPIView):
    """
    GET  /recruitments/<recruitment_id>/announcements
    POST /recruitments/<recruitment_id>/announcements
    """

    def get(self, request, recruitment_id):
        TAG = "RecruitmentAnnouncementsAPIView.get"
        try:
            actor = getattr(request, "actor", None)

            # The SAME visibility-aware selector the detail view uses, so
            # there is one answer to "may this caller see this posting" and
            # not two that drift. A posting they cannot see has no
            # announcements they can see either.
            recruitment = RecruitmentSelector.get_recruitment_detail(
                recruitment_id=recruitment_id, actor=actor
            )
            if not recruitment:
                return response_data(
                    success=False,
                    message="Recruitment not found",
                    status_code=status.HTTP_404_NOT_FOUND,
                )

            try:
                limit = min(int(request.query_params.get("limit", 20)), 50)
                offset = max(int(request.query_params.get("offset", 0)), 0)
            except (ValueError, TypeError):
                return response_data(
                    False, "Invalid pagination params", status_code=400
                )

            announcements, total_count, is_owner = (
                AnnouncementSelector.list_for_actor(
                    recruitment=recruitment,
                    actor=actor,
                    limit=limit,
                    offset=offset,
                )
            )
            announcements = list(announcements)

            # OWNER ONLY, and one grouped query for the whole page. A player
            # is not shown how many people got the message.
            context = {"request": request}
            if is_owner:
                context["delivery_summaries"] = (
                    AnnouncementService.delivery_summaries(
                        [announcement.id for announcement in announcements]
                    )
                )

            serializer = AnnouncementSerializer(
                announcements, many=True, context=context
            )

            return response_data(
                success=True,
                data={
                    "count": total_count,
                    "limit": limit,
                    "offset": offset,
                    "results": serializer.data,
                },
            )

        except Exception as e:
            logger.error(f"{TAG} | Error | {str(e)}")
            return response_data(
                success=False,
                message="Something went wrong",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                error=str(e),
            )

    @org_required
    def post(self, request, recruitment_id):
        TAG = "RecruitmentAnnouncementsAPIView.post"
        try:
            actor = request.actor

            recruitment = _owned_recruitment(actor, recruitment_id)
            if not recruitment:
                return response_data(
                    success=False,
                    message="Recruitment not found",
                    status_code=status.HTTP_404_NOT_FOUND,
                )

            # Role gate BEFORE the body is validated: a coach should get a
            # 403, not a critique of a payload that was never going to be
            # accepted.
            AnnouncementService.require_reviewer(actor)

            serializer = AnnouncementCreateSerializer(
                data=request.data,
                context={"request": request, "recruitment": recruitment},
            )
            serializer.is_valid(raise_exception=True)

            announcement = AnnouncementService.create(
                actor=actor,
                recruitment=recruitment,
                validated_data=serializer.validated_data,
            )

            logger.info(
                f"{TAG} | announcement={announcement.id} | "
                f"recruitment={recruitment.id} | "
                f"recipients={announcement.recipients_count}"
            )

            return response_data(
                success=True,
                # Deliberately "queued", not "sent": at this instant nothing
                # has been delivered, and the org's next read of the delivery
                # summary is what tells them it has.
                message=(
                    f"Announcement queued for "
                    f"{announcement.recipients_count} recipient(s)"
                ),
                data=AnnouncementSerializer(
                    announcement,
                    context={
                        "request": request,
                        "delivery_summaries": (
                            AnnouncementService.delivery_summaries(
                                [announcement.id]
                            )
                        ),
                    },
                ).data,
            )

        except PermissionDenied as e:
            return _forbidden(e)

        except ValidationError as e:
            return _invalid(TAG, e)

        except Exception as e:
            logger.error(f"{TAG} | Error | {str(e)}")
            return response_data(
                success=False,
                message="Something went wrong",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                error=str(e),
            )


class AnnouncementRecipientsCountAPIView(BaseAPIView):
    """
    GET /recruitments/<recruitment_id>/announcements/recipients-count
        ?audience=&session=

    The number the org sees BEFORE sending — resolved by the same function
    that writes the outbox, so it is the number that will be written.
    """

    @org_required
    def get(self, request, recruitment_id):
        TAG = "AnnouncementRecipientsCountAPIView"
        try:
            actor = request.actor

            recruitment = _owned_recruitment(actor, recruitment_id)
            if not recruitment:
                return response_data(
                    success=False,
                    message="Recruitment not found",
                    status_code=status.HTTP_404_NOT_FOUND,
                )

            AnnouncementService.require_reviewer(actor)

            serializer = RecipientsCountQuerySerializer(
                data=request.query_params
            )
            serializer.is_valid(raise_exception=True)

            session_id = serializer.validated_data.get("session")
            session = None
            if session_id is not None:
                session = recruitment.sessions.filter(id=session_id).first()
                if session is None:
                    raise ValidationError("Invalid date for this recruitment.")

            count = AnnouncementService.recipients_count(
                recruitment=recruitment,
                audience=serializer.validated_data["audience"],
                session=session,
            )

            return response_data(success=True, data={"count": count})

        except PermissionDenied as e:
            return _forbidden(e)

        except ValidationError as e:
            return _invalid(TAG, e)

        except Exception as e:
            logger.error(f"{TAG} | Error | {str(e)}")
            return response_data(
                success=False,
                message="Something went wrong",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                error=str(e),
            )


class AnnouncementDetailAPIView(BaseAPIView):
    """
    DELETE /recruitments/announcements/<announcement_id>

    Soft delete. It takes the entry off every list and RECALLS NOTHING — the
    response says so in words, because the org needs to know that before they
    decide whether deleting is worth anything to them.
    """

    @org_required
    def delete(self, request, announcement_id):
        TAG = "AnnouncementDetailAPIView.delete"
        try:
            actor = request.actor

            announcement = (
                RecruitmentAnnouncement.objects
                .select_related("recruitment")
                .filter(id=announcement_id)
                .first()
            )
            if (
                not announcement
                or str(announcement.recruitment.organization_id)
                != str(actor.organization.id)
            ):
                return response_data(
                    success=False,
                    message="Announcement not found",
                    status_code=status.HTTP_404_NOT_FOUND,
                )

            AnnouncementService.delete(actor, announcement)

            logger.info(f"{TAG} | announcement={announcement.id}")

            return response_data(
                success=True,
                message=DELETE_MESSAGE,
                data={
                    "announcement_id": str(announcement.id),
                    "is_deleted": True,
                    # Explicit rather than implied by the message: a client
                    # should be able to branch on a field, not parse copy.
                    "recalled": False,
                },
            )

        except PermissionDenied as e:
            return _forbidden(e)

        except Exception as e:
            logger.error(f"{TAG} | Error | {str(e)}")
            return response_data(
                success=False,
                message="Something went wrong",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                error=str(e),
            )
