"""
The feed the frontend's sitemap.xml is built from, mounted under /public/.

Not a sitemap itself: this returns JSON, and `goatza-frontend/src/app/sitemap.ts`
turns it into XML. The URLs are the FRONTEND's (`/profile/<username>`,
`/organization/profile/<username>`) and this server does not know how they are
spelled — the route group could be renamed tomorrow and no backend deploy
should be needed to keep the sitemap correct. So the API answers the one
question only it can answer ("which handles are publicly visible, and when did
they last change") and Next.js owns the URL shape.

WHAT IS NOT IN HERE, deliberately:

  * Recruitment detail pages. They live in the AUTHENTICATED route group and
    robots.ts disallows /recruitments — putting them in a sitemap would be
    asking Google to crawl a login redirect. Recruitments are surfaced publicly
    through the org profile they hang off, so the ORG is the indexable unit.
  * Anything but a handle and a timestamp. Same allow-list rule as every other
    view on this surface: no names, no ids, no counts. A sitemap is a list of
    URLs, and a URL is the one thing about these profiles that is already
    public by construction.

WHO CALLS IT: the frontend's sitemap route, hourly, from the server. Not
browsers. That is why the response is a single cached blob rather than
something paginated.
"""

import logging

from core.selectors.public_profile_selectors import (
    public_organization_sitemap_rows,
    public_user_sitemap_rows,
)
from core.views.base_views import PublicAPIView
from utils.cache import cache_get, cache_set
from utils.cache_keys import CacheKeys
from utils.response import response_data

logger = logging.getLogger(__name__)

# An hour, matching the `revalidate: 3600` on the frontend's fetch. Two full
# table scans capped at 5,000 rows each is not a query to run per crawl, and a
# profile that becomes public is discovered within the hour either way — a
# sitemap is a hint to a crawler that will take days to act on it, so freshness
# here is worth nothing and the query cost is real.
SITEMAP_TTL = 3600


class PublicSitemapURLsAPIView(PublicAPIView):
    """
    GET /public/sitemap/urls

        {"users":         [{"username": "...", "updated_at": "ISO8601"}],
         "organizations": [{"username": "...", "updated_at": "ISO8601"}]}

    Newest-first and capped per list (see ``SITEMAP_MAX_ROWS``). Anonymous, on
    PublicAPIView's standard read throttle — nothing here is more sensitive
    than the profile pages it points at, all of which are already anonymous
    reads.
    """

    def get(self, request):
        TAG = "PublicSitemapURLsAPIView"

        try:
            cache_key = CacheKeys.public_sitemap_urls()

            cached = cache_get(cache_key)
            if cached is not None:
                return response_data(success=True, data=cached)

            data = {
                "users": _serialize(public_user_sitemap_rows()),
                "organizations": _serialize(
                    public_organization_sitemap_rows()
                ),
            }

            cache_set(cache_key, data, timeout=SITEMAP_TTL)

            return response_data(success=True, data=data)

        except Exception as e:
            logger.error(f"{TAG} | Error | {str(e)}")
            return response_data(
                success=False,
                message="Something went wrong",
                status_code=500,
            )


def _serialize(rows):
    """
    Two keys per row and nothing else — the allow-list, built by hand.

    ``isoformat()`` rather than a serializer field: these are already plain
    dicts off a ``.values()`` query, and a DRF serializer here would only add a
    pass over 10,000 rows to produce the same two strings. Timestamps are
    stored in UTC (USE_TZ), so the "+00:00" offset is real and W3C-datetime
    valid, which is what <lastmod> requires.
    """
    return [
        {
            "username": row["username"],
            "updated_at": row["profile_updated_at"].isoformat(),
        }
        for row in rows
    ]
