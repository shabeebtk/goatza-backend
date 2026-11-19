"""
Featured players — "players we developed". ``page.manage``.

OPT-IN ONLY: a row exists because the academy picked the player. Picking needs
a candidate (a VERIFIED career entry at this org) who may be featured
(``is_featurable``: an active account, a public profile, no parent's approval
outstanding, no block with the org); anyone else is ``not_eligible``, one
answer for every reason. Max 60.
"""

import logging

from django.db import IntegrityError, transaction
from django.db.models import Max

from apps.orgpages import constants as C
from apps.orgpages.errors import PageError, not_eligible, not_found
from apps.orgpages.models import FeaturedPlayer
from apps.orgpages.selectors.player_selectors import featurable_entry
from apps.orgpages.services.cleaning import clean_ids, clean_text
from apps.orgpages.services.guards import (
    lock_page,
    relock_actor,
    require_page_permission,
)
from apps.orgpages.services.public_cache import bust_public_page
from utils.validations import is_valid_uuid

logger = logging.getLogger(__name__)

PLAYER_NOT_FOUND = "This player wasn't found on your page."
ALREADY_ADDED_MESSAGE = "This player is already on your page."


def _clean_highlight(payload):
    return clean_text(
        payload.get("highlight"), "highlight", label="Highlight", max_len=C.HIGHLIGHT_MAX
    )


def get_org_featured(organization, featured_id):
    row = (
        FeaturedPlayer.objects.filter(id=featured_id, organization=organization).first()
        if is_valid_uuid(str(featured_id)) else None
    )
    if row is None:
        raise not_found(PLAYER_NOT_FOUND)
    return row


class FeaturedPlayerService:

    @staticmethod
    def add(actor, payload):
        """POST page/players ``{user_id, highlight?}``."""
        organization, member = require_page_permission(actor)
        payload = payload if isinstance(payload, dict) else {}

        user_id = payload.get("user_id")
        if not is_valid_uuid(str(user_id)):
            raise not_eligible()
        highlight = _clean_highlight(payload)

        with transaction.atomic():
            lock_page(organization)
            relock_actor(member)

            rows = FeaturedPlayer.objects.filter(organization=organization)
            if rows.filter(user_id=user_id).exists():
                raise PageError("already_added", ALREADY_ADDED_MESSAGE, field="user_id")

            entry = featurable_entry(organization, actor, user_id)
            if entry is None:
                raise not_eligible()

            if rows.count() >= C.MAX_FEATURED_PLAYERS:
                raise PageError(
                    "player_limit",
                    f"You can feature up to {C.MAX_FEATURED_PLAYERS} players.",
                )

            highest = rows.aggregate(highest=Max("order"))["highest"]
            try:
                with transaction.atomic():
                    row = FeaturedPlayer.objects.create(
                        organization=organization,
                        user=entry.user,
                        career_entry=entry,
                        highlight=highlight,
                        order=0 if highest is None else highest + 1,
                    )
            except IntegrityError:
                raise PageError("already_added", ALREADY_ADDED_MESSAGE, field="user_id")
            bust_public_page(organization.id)

        logger.info("[PAGE] player featured | row=%s | org=%s", row.id, organization.id)
        return row

    @staticmethod
    def update(actor, featured_id, payload):
        """PATCH page/players/<id> ``{highlight}``."""
        organization, member = require_page_permission(actor)
        payload = payload if isinstance(payload, dict) else {}
        highlight = _clean_highlight(payload) if "highlight" in payload else None

        with transaction.atomic():
            lock_page(organization)
            relock_actor(member)

            row = get_org_featured(organization, featured_id)
            if highlight is not None and highlight != row.highlight:
                row.highlight = highlight
                row.save(update_fields=["highlight"])
                bust_public_page(organization.id)

        return row

    @staticmethod
    def remove(actor, featured_id):
        organization, member = require_page_permission(actor)

        with transaction.atomic():
            lock_page(organization)
            relock_actor(member)

            get_org_featured(organization, featured_id).delete()
            bust_public_page(organization.id)

    @staticmethod
    def reorder(actor, payload):
        """``{ids}`` must be exactly the org's picked rows."""
        organization, member = require_page_permission(actor)

        with transaction.atomic():
            lock_page(organization)
            relock_actor(member)

            rows = {
                str(row.id): row
                for row in FeaturedPlayer.objects.filter(organization=organization)
            }
            ids = clean_ids(payload, rows)

            changed = []
            for index, row_id in enumerate(ids):
                row = rows[row_id]
                if row.order != index:
                    row.order = index
                    changed.append(row)
            if changed:
                FeaturedPlayer.objects.bulk_update(changed, ["order"])
                bust_public_page(organization.id)
