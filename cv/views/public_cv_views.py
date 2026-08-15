"""
The anonymous-reachable Sports CV, mounted at /public/cv/<username> from
core.public_urls.

Same two design points as its sibling in core/views/public_profile_views.py —
one bundle so the server-rendered page paints in one round trip, and 404 never
403 so a disabled CV, a hidden profile, a coach's username and a typo are
indistinguishable.

The order of operations in ``get`` is load-bearing and is spelled out inline.
"""

import logging

from core.views.base_views import PublicAPIView
from core.views.public_profile_views import PUBLIC_BUNDLE_TTL
from cv.selectors.cv_selectors import cv_payload, get_cv_user
from cv.services.cv_services import CVService
from utils.cache import cache_get, cache_set
from utils.cache_keys import CacheKeys
from utils.response import response_data

logger = logging.getLogger(__name__)


class PublicCVAPIView(PublicAPIView):
    """
    GET /public/cv/<username>

    Bundle: header + sport + career + achievements + highlights, minus whatever
    the owner switched off.
    """

    def get(self, request, username):
        TAG = "PublicCVAPIView"

        try:
            # 1. Resolve first, always — before the cache, before anything.
            #    This is the only step that can 404, and it is the step that
            #    proves the CV is still meant to be public.
            resolved = get_cv_user(username)
            if resolved is None:
                return response_data(
                    success=False,
                    message="CV not found",
                    status_code=404,
                )

            user, settings = resolved

            # 2. Count the view BEFORE the cache check. A shared CV is served
            #    from cache almost every time; counting after the early return
            #    below would mean the counter silently stopped working the
            #    moment the feature got popular. The service deduplicates per
            #    IP, so this is not a refresh-loop amplifier.
            CVService.record_view(settings, request)

            actor = request.actor

            # 3. Only the anonymous rendering is cacheable — and here the
            #    payload happens to be viewer-independent, so a signed-in
            #    caller would in fact read the same bytes. The rule is kept
            #    anyway: it is the same rule as every other public bundle, and
            #    the day a viewer-dependent field is added, this file should
            #    not be the one that has to remember.
            cache_key = CacheKeys.public_cv(username)
            if actor is None:
                cached = cache_get(cache_key)
                if cached is not None:
                    return response_data(success=True, data=cached)

            data = cv_payload(user, settings, actor)

            if actor is None:
                cache_set(cache_key, data, timeout=PUBLIC_BUNDLE_TTL)

            return response_data(success=True, data=data)

        except Exception as e:
            logger.error(f"{TAG} | Error | username={username} | {str(e)}")
            return response_data(
                success=False,
                message="Something went wrong",
                status_code=500,
            )
