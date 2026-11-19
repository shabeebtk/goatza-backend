"""
Clearing the anonymous org page cache.

GET /public/organization/<username>/page is cached for 60 s for anonymous
callers, like the org bundle and the squads showcase. Every write that changes
what it shows calls ``bust_public_page``: contact options, facts, section
switches, programs, facilities, coaches, featured players — and, from outside
this app, the org's public-profile setting, its profile, its locations and its
sports. Keyed by username, so the org is read again after commit (one small
query) rather than trusted from a row loaded earlier.
"""

import logging

from django.db import transaction

from utils.cache import cache_delete
from utils.cache_keys import CacheKeys

logger = logging.getLogger(__name__)


def _bust(organization_id):
    from apps.organization.models import Organization

    username = (
        Organization.objects.filter(id=organization_id)
        .values_list("username", flat=True)
        .first()
    )
    if username:
        cache_delete(CacheKeys.public_org_page(username))


def bust_public_page(organization_id):
    """
    After commit, and never allowed to fail the write it follows. Outside a
    transaction Django runs it at once.

    A lambda, never ``functools.partial``: ``robust=True`` logs a failure with
    the callback's ``__qualname__``, which a partial does not have.
    """
    if organization_id:
        transaction.on_commit(lambda: _bust(organization_id), robust=True)
