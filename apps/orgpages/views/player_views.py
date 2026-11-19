"""Featured players, as the club picks them. ``page.manage``."""

from apps.orgpages.selectors.player_selectors import editor_players
from apps.orgpages.services.guards import require_page_permission
from apps.orgpages.services.player_service import FeaturedPlayerService
from apps.orgpages.views.base import PageBaseView, body_of
from utils.response import response_data


def players_response(request, *, message="", status_code=200, q=None):
    return response_data(
        success=True,
        message=message,
        data=editor_players(request.actor.organization, request.actor, q=q),
        status_code=status_code,
    )


class PagePlayersAPIView(PageBaseView):
    """
    GET /page/players ?q= → {players, candidates}
    POST /page/players {user_id, highlight?} → 201 {players, candidates}
    """

    def get(self, request):
        require_page_permission(request.actor)
        q = (request.query_params.get("q") or "").strip()[:80] or None
        return players_response(request, q=q)

    def post(self, request):
        FeaturedPlayerService.add(request.actor, body_of(request))
        return players_response(request, message="Player added", status_code=201)


class PagePlayerDetailAPIView(PageBaseView):
    """PATCH /page/players/<uuid> {highlight} → {players, candidates}"""

    def patch(self, request, featured_id):
        FeaturedPlayerService.update(request.actor, featured_id, body_of(request))
        return players_response(request, message="Player saved")


class PagePlayerRemoveAPIView(PageBaseView):
    """POST /page/players/<uuid>/remove → {players, candidates}"""

    def post(self, request, featured_id):
        FeaturedPlayerService.remove(request.actor, featured_id)
        return players_response(request, message="Player removed")


class PagePlayerReorderAPIView(PageBaseView):
    """POST /page/players/reorder {ids} → {players, candidates}"""

    def post(self, request):
        FeaturedPlayerService.reorder(request.actor, body_of(request))
        return players_response(request, message="Order saved")
