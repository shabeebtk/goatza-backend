"""
Which posts on a profile a given actor may see.

Lifted verbatim out of ``posts.views.posts_views.ListPostsAPIView`` so the
authenticated profile list and the public (logged-out) profile list share one
rule instead of two copies that drift. The behaviour for a signed-in actor is
unchanged; the only addition is that ``actor`` may now be None.

An anonymous actor degrades correctly on its own once
``FollowService.get_following_ids`` is None-safe: they follow nobody, so the
FOLLOWERS branch never opens, and they author nothing, so the own-posts branch
is skipped. What is left is exactly ``visibility=PUBLIC``.
"""

from django.db.models import Q

from connections.services.follow_services import FollowService
from core.constant import TYPE_USER
from posts.models import Post


def profile_visibility_filter(actor, profile=None):
    """
    The Q() to AND onto a profile's post queryset.

    ``profile`` is the ``{"type", "id"}`` dict from
    ``UserOrganizationService.get_user_or_org_by_username`` — the author whose
    profile is being viewed — or None when listing by post_id alone.

    Costs the one follow query ``get_following_ids`` already made, and none for
    an anonymous caller.
    """
    visibility_filter = Q(visibility=Post.Visibility.PUBLIC)

    following_ids = FollowService.get_following_ids(actor)

    # Followers-only posts, but only from the profile actually being viewed —
    # this is a profile list, not a feed, so there is one author to check.
    if profile:
        if profile["type"] == TYPE_USER:
            if profile["id"] in following_ids["user_ids"]:
                visibility_filter |= Q(
                    visibility=Post.Visibility.FOLLOWERS,
                    author_user_id=profile["id"],
                )
        else:
            if profile["id"] in following_ids["org_ids"]:
                visibility_filter |= Q(
                    visibility=Post.Visibility.FOLLOWERS,
                    author_org_id=profile["id"],
                )

    # Own posts are always visible to their author, whatever the setting.
    if actor is None:
        return visibility_filter

    if actor.is_user:
        visibility_filter |= Q(author_user=actor.user)
    else:
        visibility_filter |= Q(author_org=actor.organization)

    return visibility_filter
