"""
Visibility rules for content shared into a conversation.

Two different callers need the same answer:

  * MessageService — "may this SENDER share this object?" (once per send)
  * MessageSerializer — "may this VIEWER see this preview?" (once per message
    in a page)

Both go through ``ShareViewer``, which caches the actor's follow graph and block
list so rendering a page of N shared messages costs at most two follow queries and
one block lookup in total rather than per message (see the assertNumQueries test).

The rules mirror the ones already enforced elsewhere and are kept here, in one
place, because sharing needs a combination no existing selector offers:

  * posts   — same rule as feed_services._visibility_filter (public, or
              followers-only from someone the actor follows, or own post).
  * recruitments — RecruitmentSelector's owner/public/followers_only rule. Two
              questions, because a card already sent outlives the trial it is for:
              ``can_view_shared_recruitment`` for previews (CLOSED is fine, the
              card just says so) and ``can_share_recruitment`` for new shares
              (only while it can still be applied to).
  * profiles  — reachability only (see is_user_profile_shareable). Profiles
              have no followers-only tier, so this path never consults the
              follow graph.

Every rule also answers no for content whose owner has gone (a deactivated
account, a suspended org) and across a block in either direction: never a 500, and
never half a card. The block is silent on purpose, the same ``unavailable`` as
anything else, so a blocked person cannot tell why.
"""

from django.utils.functional import cached_property

from apps.accounts.models import User
from apps.connections.models import Follow
from apps.moderation.selectors.blocked_filters import blocked_id_sets
from apps.moderation.services.block_guard import as_actor
from apps.posts.models import Post
from apps.recruitments.models import Recruitment

COUNTERPART_DELETED_MESSAGE = "This account has been deleted."
COUNTERPART_INACTIVE_MESSAGE = "This account is no longer active."
COUNTERPART_ORG_INACTIVE_MESSAGE = "This organization is no longer active."


class ShareViewer:
    """
    The actor a share is being validated against — either the sender (at send
    time) or the reader of a message list (at render time). Exactly one of
    user/org is set, per the dual-actor rule.
    """

    def __init__(self, user=None, org=None):
        self.user = user
        self.org = org

    @classmethod
    def from_actor(cls, actor):
        """Build from a core.actor.Actor (request.actor / scope["actor"])."""
        if actor is None:
            return cls()
        return cls(
            user=actor.user if actor.is_user else None,
            org=actor.organization if actor.is_org else None,
        )

    @property
    def is_anonymous(self):
        return self.user is None and self.org is None

    # ── follow graph (loaded once, on first use) ─────────────────

    @cached_property
    def _follows(self):
        if self.is_anonymous:
            return {"users": set(), "orgs": set()}

        if self.user:
            queryset = Follow.objects.filter(follower_user=self.user)
        else:
            queryset = Follow.objects.filter(follower_org=self.org)

        rows = queryset.values_list("following_user_id", "following_org_id")

        return {
            "users": {u for u, _ in rows if u},
            "orgs": {o for _, o in rows if o},
        }

    def follows_user(self, user_id):
        return bool(user_id) and user_id in self._follows["users"]

    def follows_org(self, org_id):
        return bool(org_id) and org_id in self._follows["orgs"]

    # ── blocks, in either direction (loaded once, on first use) ──

    @cached_property
    def _blocked(self):
        if self.is_anonymous:
            return set(), set()

        return blocked_id_sets(as_actor(self.user or self.org))

    def blocked_with_user(self, user_id):
        return bool(user_id) and user_id in self._blocked[0]

    def blocked_with_org(self, org_id):
        return bool(org_id) and org_id in self._blocked[1]

    def owns_post(self, post):
        return bool(
            (self.user and post.author_user_id == self.user.id)
            or (self.org and post.author_org_id == self.org.id)
        )

    def owns_recruitment(self, recruitment):
        return bool(self.org and recruitment.organization_id == self.org.id)


def _org_is_reachable(org) -> bool:
    return bool(org is not None and org.is_active and not org.is_suspended)


def is_deleted_account(user) -> bool:
    """
    The owner deleted it: waiting for the purge, or already anonymized by it. A
    staff suspension also clears ``is_active``, but leaves the rest alone.
    """
    return user.deletion_requested_at is not None or (
        not user.is_active and not user.username
    )


def counterpart_unavailable_reason(identity):
    """
    Why nobody can message this User or Organization, as the sentence to show, or
    None when they can. ``identity`` None means the row is gone altogether.

    Suspended reads the same as deactivated: that is a moderation decision about
    somebody else, and not this person's to be told.
    """
    if identity is None:
        return COUNTERPART_DELETED_MESSAGE

    if isinstance(identity, User):
        if is_deleted_account(identity):
            return COUNTERPART_DELETED_MESSAGE
        return None if identity.is_active else COUNTERPART_INACTIVE_MESSAGE

    return None if _org_is_reachable(identity) else COUNTERPART_ORG_INACTIVE_MESSAGE


def is_post_shareable(post, viewer: ShareViewer) -> bool:
    """
    True when ``viewer`` may see — and therefore share, or be shown a preview
    of — ``post``. Soft-deleted posts are never shareable, and neither are the
    posts of an account or org that has gone.
    """
    if post is None or post.is_deleted:
        return False

    if post.author_user_id:
        if not post.author_user.is_active:
            return False
        if viewer.blocked_with_user(post.author_user_id):
            return False
    else:
        if not _org_is_reachable(post.author_org):
            return False
        if viewer.blocked_with_org(post.author_org_id):
            return False

    if post.visibility == Post.Visibility.PUBLIC:
        return True

    if post.visibility == Post.Visibility.FOLLOWERS:
        if viewer.owns_post(post):
            return True
        return (
            viewer.follows_user(post.author_user_id)
            or viewer.follows_org(post.author_org_id)
        )

    return False


def is_recruitment_visible_to(recruitment, viewer: ShareViewer) -> bool:
    """
    Who may know this recruitment exists, whatever state it is in: the org must
    still be reachable, no block between them, and its visibility has to allow it.
    """
    if not _org_is_reachable(recruitment.organization):
        return False

    if viewer.blocked_with_org(recruitment.organization_id):
        return False

    # The owning org can always see its own listing.
    if viewer.owns_recruitment(recruitment):
        return True

    if recruitment.visibility == Recruitment.Visibility.PUBLIC:
        return True

    if recruitment.visibility == Recruitment.Visibility.FOLLOWERS_ONLY:
        return viewer.follows_org(recruitment.organization_id)

    # PRIVATE — owner only, handled above.
    return False


def can_view_shared_recruitment(recruitment, viewer: ShareViewer) -> bool:
    """
    True when ``viewer`` may be shown the card of a recruitment shared to them.
    Soft-deleted, draft and cancelled ones never; closed ones yes, because the
    message was already sent and the card can say it has closed.
    """
    if recruitment is None or recruitment.is_deleted:
        return False

    if recruitment.status not in (
        Recruitment.Status.ACTIVE,
        Recruitment.Status.CLOSED,
    ):
        return False

    return is_recruitment_visible_to(recruitment, viewer)


def can_share_recruitment(recruitment, viewer: ShareViewer) -> bool:
    """
    True when ``viewer`` may forward ``recruitment`` as a NEW share: it is live, can
    still be applied to, and they may see it. A card for a trial nobody can join
    only sends the next person to a dead end. The live checks read columns, so this
    is safe once per row of a list.
    """
    return bool(
        recruitment is not None
        and not recruitment.is_deleted
        and recruitment.status == Recruitment.Status.ACTIVE
        and recruitment.is_accepting_applications
        and not recruitment.is_trial_over
        and is_recruitment_visible_to(recruitment, viewer)
    )


def is_user_profile_shareable(user, viewer: ShareViewer) -> bool:
    """
    True when ``viewer`` may forward — or be shown a preview card of —
    ``user``'s profile.

    Reachability, not visibility. The questions are whether the account still
    exists, is still active, has a username (``User.username`` is nullable, and a
    profile with no handle has no URL to open), and is not on the other side of a
    block:

      * ``is_public_profile`` is DELIBERATELY not checked. That flag governs the
        logged-out web view only. Inside Goatza every signed-in actor can
        already see every profile, so a hidden profile stays shareable in DMs
        and its preview card renders normally — refusing here would break a
        capability the recipient already has, and would leak the owner's
        privacy setting to whoever tried.

      * No follow lookup. Profiles have no followers-only tier, so there is
        nothing a follow could unlock, and reading ``ShareViewer._follows`` here
        would fire a follow query per share for an answer that cannot change the
        outcome.
    """
    return bool(
        user is not None
        and user.is_active
        and user.username
        and not viewer.blocked_with_user(user.id)
    )


def is_org_profile_shareable(org, viewer: ShareViewer) -> bool:
    """
    True when ``viewer`` may forward ``org``'s profile. Same reasoning as the
    user twin; ``Organization.username`` is required by the model, so that one
    check has no twin here.
    """
    return bool(
        _org_is_reachable(org) and not viewer.blocked_with_org(org.id)
    )
