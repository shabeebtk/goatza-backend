"""
The anonymous-reachable recruitment detail, mounted at
/public/recruitments/<recruitment_id> from core.public_urls.

A trial posting is the thing this product is shared for: a club puts one up,
somebody drops the link into a WhatsApp group, and every person in that group
who does not yet have an account hits this endpoint. So it exists for the same
reason the public profile does — a link with nothing behind it does not get
opened — and it plays by the same two rules.

  * ALWAYS the public serializer. Not "the public one unless the caller turns
    out to be the owner": see the note on the serializer choice below. This is
    the one place in the recruitment app where the owner deliberately does not
    get the owner payload.

  * 404, never 403. A draft, a cancelled posting, a followers-only one and a
    typo'd uuid are indistinguishable in the response, and the body is the same
    one RecruitmentDetailAPIView returns — a client that already handles that
    shape needs no second branch.

Visibility itself is NOT decided here. ``RecruitmentSelector
.get_recruitment_detail`` is the single home for that rule and it already
answers None for an anonymous caller unless the recruitment is ACTIVE and
public; ActorMixin hands it ``request.actor = None`` when there is no token,
and resolves a signed-in caller normally, so a follower opening this URL gets
the followers-only posting they are entitled to.
"""

import logging

from core.views.base_views import PublicAPIView
from apps.recruitments.selectors.recruitment_selectors import RecruitmentSelector
from apps.recruitments.serializers.recruitment_list_serializers import (
    RecruitmentDetailSerializer,
)
from apps.recruitments.services.recruitment_view_service import (
    RecruitmentViewService,
)
from utils.response import response_data

logger = logging.getLogger(__name__)


class PublicRecruitmentDetailAPIView(PublicAPIView):
    """
    GET /public/recruitments/<uuid:recruitment_id>
    """

    def get(self, request, recruitment_id):

        TAG = "PublicRecruitmentDetailAPIView"

        try:
            actor = request.actor

            # FETCH RECRUITMENT — the same visibility-aware selector the
            # authenticated detail view uses, so there is one answer to "may
            # this caller see this posting" and not two that can drift.
            recruitment = RecruitmentSelector.get_recruitment_detail(
                recruitment_id=recruitment_id,
                actor=actor,
            )
            if not recruitment:
                # Byte-for-byte the authed view's miss. Anything else would
                # tell a prober that a uuid they guessed is real but private.
                return response_data(
                    success=False,
                    message="Recruitment not found",
                    status_code=404,
                )

            # VIEW COUNT — after the fetch, because a 404 is not a view. The
            # service owns the owner exclusion and the per-viewer window, and
            # it can never raise; see its module docstring.
            RecruitmentViewService.record_view(recruitment, actor, request)

            # SERIALIZER — the public one, unconditionally.
            #
            # The authed twin picks RecruitmentOwnerDetailSerializer when the
            # caller turns out to own the posting. Here that would be wrong
            # twice over: this payload is cacheable and shareable by every
            # layer between us and the browser, and the owner-only fields
            # (views_count, saves_count, status, max_applications,
            # shortlisted_count, selected_count) are precisely the ones that
            # must never appear on a URL a stranger can open. An org admin who
            # wants their numbers has /recruitments/<id>/details, which is
            # authenticated and un-cacheable, and the frontend sends them
            # there.
            serializer = RecruitmentDetailSerializer(
                recruitment,
                context={"request": request},
            )

            logger.info(
                f"{TAG} | Success | recruitment_id={recruitment.id}"
            )

            return response_data(
                success=True,
                data=serializer.data,
            )

        except Exception as e:
            logger.error(
                f"{TAG} | Error | "
                f"recruitment_id={recruitment_id} | {str(e)}"
            )
            return response_data(
                success=False,
                message="Something went wrong",
                status_code=500,
            )
