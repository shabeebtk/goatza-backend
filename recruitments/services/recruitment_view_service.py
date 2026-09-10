"""
The ``views_count`` counter on a recruitment.

The number is OWNER-ONLY — it is exposed by
``RecruitmentOwnerDetailSerializer`` and by nothing else — so this module has
exactly one reader: the org that posted the listing, looking at its own
dashboard. That is what sets every rule below.

  * The owner's own opens never count. An org that edits a posting four times
    on the morning it goes live would otherwise read its own traffic back as
    interest, which is the one way this number can actively mislead.

  * One viewer counts once per window. A player who reads the trial details,
    checks the venue on a map and comes back is one interested person, not
    three; and without a latch a refresh loop is a counter anybody can move.

  * COUNTING NEVER BREAKS THE READ. Every failure here — Redis down, a
    connection reset mid-UPDATE — is swallowed with a warning. A detail page
    that 500s because a statistic could not be incremented is a page that
    fails for the worst possible reason.

The increment itself is a single ``F()`` UPDATE on the queryset rather than a
``save()`` on the instance: concurrent viewers add up instead of overwriting
each other, and the detail view has just fetched a fully-prefetched row it has
no business writing back.
"""

import logging

from django.db.models import F

from recruitments.models import Recruitment
from utils.cache import cache_add
from utils.cache_keys import CacheKeys
from utils.request_meta import client_ip

logger = logging.getLogger(__name__)


# One counted view per viewer per recruitment per SIX HOURS.
#
# Longer than the CV's half hour on purpose. A trial posting is decided on
# slowly — a player reads it, sends it to a parent, comes back that evening to
# check the reporting time — and all of that is one person's interest. Six
# hours also means a posting shared into a group chat in the morning collects
# roughly one count per person who opened it that day, which is the number the
# org is actually trying to read.
VIEW_COUNT_TTL = 6 * 60 * 60

# The bucket every anonymous caller we could not identify shares.
#
# ``client_ip`` returns None for a MISSING address and, deliberately, also for
# a malformed ``X-Forwarded-For`` — which is attacker-controlled and therefore
# the exact header someone inflating a counter would fill with junk. Pooling
# them all under one latch means the whole unidentifiable population can move
# the number once per window between them, instead of once per request.
UNKNOWN_IDENT = "ip:unknown"


class RecruitmentViewService:

    @staticmethod
    def viewer_ident(actor, request):
        """
        Who is looking, as a cache-key fragment.

        Actor identity beats IP wherever we have one: it survives a phone
        moving between wifi and mobile data, it does not pool a whole office
        behind one NAT address, and it is what makes the public detail view
        and the authenticated one agree about a viewer who opened the link
        logged out and then signed in.
        """
        if actor is not None:
            if actor.is_user:
                return f"user:{actor.user.id}"
            if actor.is_org:
                return f"org:{actor.organization.id}"

        ip = client_ip(request)

        return f"ip:{ip}" if ip else UNKNOWN_IDENT

    @staticmethod
    def is_owner(recruitment, actor):
        """
        The same owner test the detail views apply — an org actor whose
        organization posted this. Kept here so the "don't count the owner"
        rule cannot be forgotten by a future call site that has an actor but
        did not need to branch on ownership for any other reason.
        """
        return bool(
            actor
            and actor.is_org
            and str(actor.organization.id) == str(recruitment.organization_id)
        )

    @staticmethod
    def record_view(recruitment, actor, request) -> bool:
        """
        Count one read of a recruitment's detail page. Returns True when the
        counter actually moved.

        Call it AFTER the visibility-aware fetch has succeeded: a 404 is not a
        view, and a followers-only posting must not tick over for somebody the
        selector just refused.

        Every branch that returns False is a normal outcome, not an error —
        the owner looking at their own listing, a viewer already counted this
        window, a row that vanished between the fetch and the update.
        """
        TAG = "RecruitmentViewService.record_view"

        try:
            if recruitment is None:
                return False

            if RecruitmentViewService.is_owner(recruitment, actor):
                return False

            ident = RecruitmentViewService.viewer_ident(actor, request)

            # Atomic in the cache backend, unlike get-then-set, which is what
            # makes it usable as a "count this once" latch when the same
            # person's two tabs land at the same moment.
            if not cache_add(
                CacheKeys.recruitment_view_counted(recruitment.id, ident),
                1,
                VIEW_COUNT_TTL,
            ):
                return False

            updated = Recruitment.objects.filter(pk=recruitment.pk).update(
                views_count=F("views_count") + 1
            )

            if updated:
                # Keep the in-memory row honest for anything serialized after
                # this call. Cheap, and it saves a re-read of a number that is
                # approximate by design.
                recruitment.views_count = (recruitment.views_count or 0) + 1

            return bool(updated)

        except Exception as e:
            # Deliberately swallowed — see the module docstring. The viewer
            # gets their page; we get a line in the log.
            logger.warning(
                f"{TAG} | Skipped | "
                f"recruitment_id={getattr(recruitment, 'id', None)} | {str(e)}"
            )
            return False
