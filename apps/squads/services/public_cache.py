"""
Clearing the anonymous showcase cache.

The two public routes are cached for 60 s for anonymous callers, like the org
bundle. Every write that changes what they show calls ``bust_public_squads``:
a squad created, edited, hidden or deleted; a player joining, leaving, moving
or getting a new number; staff added or removed; the org's own public-profile
setting. Keyed by username, so the org is read again after commit — one small
query — rather than trusted from a row loaded earlier.

All of the org's squad pages go at once, not just the one that changed: each
page lists the club's other squads with their counts.
"""

import logging

from django.db import transaction

from utils.cache import cache_delete_many
from utils.cache_keys import CacheKeys

logger = logging.getLogger(__name__)


def _bust(organization_id):
    from apps.organization.models import Organization
    from apps.squads.models import Squad

    username = (
        Organization.objects.filter(id=organization_id)
        .values_list("username", flat=True)
        .first()
    )
    if not username:
        return

    squad_ids = Squad.objects.filter(organization_id=organization_id).values_list(
        "id", flat=True
    )
    cache_delete_many(
        [CacheKeys.public_org_squads(username)]
        + [CacheKeys.public_org_squad(username, squad_id) for squad_id in squad_ids]
    )


def bust_public_squads(organization_id):
    """After commit, and never allowed to fail the write it follows."""
    if organization_id:
        transaction.on_commit(lambda: _bust(organization_id), robust=True)
