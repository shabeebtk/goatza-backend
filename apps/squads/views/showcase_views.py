"""
The squad showcase — what everyone else sees of a club's squads.

Two surfaces over ONE set of rules (``selectors.showcase_selectors``):

  * Signed in: ``/squads/showcase/<org_username>[/<squad_id>]``, any actor.
    Photos as everywhere in the app; blocks applied.
  * Anonymous: ``/public/organization/<username>/squads[/<squad_id>]``
    (routed from core/public_urls.py). Private profiles counted but not
    listed, no minor's photo, the org's own public-profile setting obeyed.
    Its answer never depends on who is asking — it is the shareable,
    cacheable rendering — and anonymous callers are served from a 60 s cache
    exactly like the org bundle, cleared by ``bust_public_squads``.

Every miss is one 404. The club's own members get no exception: hidden
squads are only reachable through the club endpoints.
"""

from apps.squads.errors import SquadError
from apps.squads.selectors.showcase_selectors import (
    showcase_organization,
    showcase_squad,
    visible_squads,
)
from apps.squads.serializers.showcase_serializers import (
    showcase_list_payload,
    showcase_squad_payload,
)
from apps.squads.views.base import SquadBaseView, SquadErrorsMixin
from core.views.base_views import PublicAPIView
from utils.cache import cache_get, cache_set
from utils.cache_keys import CacheKeys
from utils.response import response_data

PUBLIC_SHOWCASE_TTL = 60


def _not_found():
    return SquadError("not_found", "This page wasn't found.", status_code=404)


def _list(username, actor, *, anonymous):
    organization = showcase_organization(username, actor)
    if organization is None:
        raise _not_found()
    return showcase_list_payload(
        organization, visible_squads(organization, actor), anonymous=anonymous
    )


def _squad(username, squad_id, actor, *, anonymous):
    organization = showcase_organization(username, actor)
    if organization is None:
        raise _not_found()

    entry, others = showcase_squad(organization, squad_id, actor)
    if entry is None:
        raise _not_found()

    return showcase_squad_payload(organization, entry, others, anonymous=anonymous)


class SquadShowcaseListAPIView(SquadBaseView):
    """GET /squads/showcase/<org_username>"""

    def get(self, request, org_username):
        return response_data(
            success=True,
            data=_list(org_username, request.actor, anonymous=False),
        )


class SquadShowcaseDetailAPIView(SquadBaseView):
    """GET /squads/showcase/<org_username>/<uuid>"""

    def get(self, request, org_username, squad_id):
        return response_data(
            success=True,
            data=_squad(org_username, squad_id, request.actor, anonymous=False),
        )


class PublicSquadShowcaseListAPIView(SquadErrorsMixin, PublicAPIView):
    """GET /public/organization/<username>/squads"""

    def get(self, request, username):
        cacheable = request.actor is None
        key = CacheKeys.public_org_squads(username)

        if cacheable:
            cached = cache_get(key)
            if cached is not None:
                return response_data(success=True, data=cached)

        data = _list(username, None, anonymous=True)

        if cacheable:
            cache_set(key, data, timeout=PUBLIC_SHOWCASE_TTL)

        return response_data(success=True, data=data)


class PublicSquadShowcaseDetailAPIView(SquadErrorsMixin, PublicAPIView):
    """GET /public/organization/<username>/squads/<uuid>"""

    def get(self, request, username, squad_id):
        cacheable = request.actor is None
        key = CacheKeys.public_org_squad(username, squad_id)

        if cacheable:
            cached = cache_get(key)
            if cached is not None:
                return response_data(success=True, data=cached)

        data = _squad(username, squad_id, None, anonymous=True)

        if cacheable:
            cache_set(key, data, timeout=PUBLIC_SHOWCASE_TTL)

        return response_data(success=True, data=data)
