"""
The org page as everyone else sees it.

Two surfaces over ONE set of rules (``selectors.page_selectors``):

  * Signed in: ``/page/showcase/<org_username>``, any actor. Blocks applied;
    photos as everywhere in the app.
  * Anonymous: ``/public/organization/<username>/page`` (routed from
    core/public_urls.py). Its answer never depends on who is asking — it is
    the shareable, cacheable rendering — so it is always built as anonymous,
    and anonymous callers are served from a 60 s cache like the org bundle,
    cleared by ``bust_public_page``.

Every miss is one 404. The club's own members get no exception: a
switched-off section stays hidden here.
"""

from apps.orgpages.errors import not_found
from apps.orgpages.selectors.page_selectors import page_for, page_organization
from apps.orgpages.views.base import PageBaseView, PageErrorsMixin
from core.views.base_views import PublicAPIView
from utils.cache import cache_get, cache_set
from utils.cache_keys import CacheKeys
from utils.response import response_data

PUBLIC_PAGE_TTL = 60
PAGE_NOT_FOUND = "This page wasn't found."


def _page(username, actor):
    organization = page_organization(username, actor)
    if organization is None:
        raise not_found(PAGE_NOT_FOUND)
    return page_for(organization, actor)


class PageShowcaseAPIView(PageBaseView):
    """GET /page/showcase/<org_username>"""

    def get(self, request, org_username):
        return response_data(success=True, data=_page(org_username, request.actor))


class PublicPageAPIView(PageErrorsMixin, PublicAPIView):
    """GET /public/organization/<username>/page"""

    def get(self, request, username):
        cacheable = request.actor is None
        key = CacheKeys.public_org_page(username)

        if cacheable:
            cached = cache_get(key)
            if cached is not None:
                return response_data(success=True, data=cached)

        data = _page(username, None)

        if cacheable:
            cache_set(key, data, timeout=PUBLIC_PAGE_TTL)

        return response_data(success=True, data=data)
