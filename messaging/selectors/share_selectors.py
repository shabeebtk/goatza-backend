"""
Visibility rules for content shared into a conversation.

Two different callers need the same answer:

  * MessageService — "may this SENDER share this object?" (once per send)
  * MessageSerializer — "may this VIEWER see this preview?" (once per message
    in a page)

Both go through ``ShareViewer``, which caches the actor's follow graph so
rendering a page of N shared messages costs at most two follow queries in
total rather than two per message (see the assertNumQueries test).

The rules mirror the ones already enforced elsewhere and are kept here, in one
place, because sharing needs a combination no existing selector offers:

  * posts   — same rule as feed_services._visibility_filter (public, or
              followers-only from someone the actor follows, or own post).
  * recruitments — RecruitmentSelector's owner/public/followers_only rule, but
              CLOSED is allowed as well as ACTIVE. A closed trial is still
              worth forwarding ("look what I missed"); a draft or cancelled one
              is not, and is never shareable.
"""

from django.utils.functional import cached_property

from connections.models import Follow
from posts.models import Post
from recruitments.models import Recruitment


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

    def owns_post(self, post):
        return bool(
            (self.user and post.author_user_id == self.user.id)
            or (self.org and post.author_org_id == self.org.id)
        )

    def owns_recruitment(self, recruitment):
        return bool(self.org and recruitment.organization_id == self.org.id)


def is_post_shareable(post, viewer: ShareViewer) -> bool:
    """
    True when ``viewer`` may see — and therefore share, or be shown a preview
    of — ``post``. Soft-deleted posts are never shareable.
    """
    if post is None or post.is_deleted:
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


def is_recruitment_shareable(recruitment, viewer: ShareViewer) -> bool:
    """
    True when ``viewer`` may see — and therefore share, or be shown a preview
    of — ``recruitment``. Soft-deleted, draft and cancelled recruitments are
    never shareable; closed ones are (the listing still has value once the
    deadline has passed).
    """
    if recruitment is None or recruitment.is_deleted:
        return False

    if recruitment.status not in (
        Recruitment.Status.ACTIVE,
        Recruitment.Status.CLOSED,
    ):
        return False

    # The owning org can always share its own listing.
    if viewer.owns_recruitment(recruitment):
        return True

    if recruitment.visibility == Recruitment.Visibility.PUBLIC:
        return True

    if recruitment.visibility == Recruitment.Visibility.FOLLOWERS_ONLY:
        return viewer.follows_org(recruitment.organization_id)

    # PRIVATE — owner only, handled above.
    return False
