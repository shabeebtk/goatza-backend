"""
Read queries behind the public (logged-out) profile surface.

Lives in ``core`` rather than in ``accounts``/``organization`` because one
bundle spans five apps — profile, sports, careers, achievements, highlights and
posts — and putting it in any one of them would make that app import the other
four. ``core.public_urls`` is the matching single place the routes are declared,
so the whole anonymous-reachable surface is two files.

Nothing here re-implements a visibility rule. Each domain already owns one and
each already tolerates ``actor=None``:

  * posts        → ``posts.selectors.post_visibility_selectors``
  * highlights   → ``highlights.selectors.highlight_selectors``
  * recruitments → the ACTIVE + PUBLIC pair, which is what
                   ``RecruitmentSelector`` gives a non-owner anyway
  * careers / achievements → no matrix at all; both lists are fully public to
                   anyone who can see the profile, verified rows just carry a
                   badge (see their selectors' docstrings)

A hidden, deactivated or usernameless profile resolves to None here, and the
view turns that into a 404 — never a 403, which would confirm it exists.
"""

from accounts.models import User
from achievements.selectors.achievement_selectors import (
    list_for_user as achievements_for_user,
)
from careers.selectors.career_selectors import career_entries_for
from core.constant import TYPE_ORGANIZATION, TYPE_USER
from highlights.selectors.highlight_selectors import visible_highlights_for
from organization.models import Organization
from posts.models import Post
from posts.selectors.post_visibility_selectors import profile_visibility_filter
from posts.serializers.posts_serializers import POST_MENTIONS_PREFETCH
from posts.services.saved_post_service import annotate_is_saved
from recruitments.models import Recruitment

# How many posts ride along in the bundle. One screenful — enough that the
# server-rendered page is complete on first paint, few enough that a profile
# with thousands of posts doesn't pay for them on every crawl.
PUBLIC_POSTS_PAGE_SIZE = 10
PUBLIC_POSTS_MAX_LIMIT = 30

# Active public listings shown on an org's public profile.
PUBLIC_RECRUITMENTS_LIMIT = 10


# ─────────────────────────────────────────────
# RESOLUTION
# ─────────────────────────────────────────────

def get_public_user(username):
    """
    The user behind a public profile URL, or None.

    Three separate reasons to return None, all of which the view reports as the
    same 404 — a visitor must not be able to tell a hidden profile from a
    deactivated one from a typo:

      * no such username
      * ``is_active`` False (deactivated / suspended)
      * ``profile.is_public_profile`` False (owner opted out)

    ``User.username`` is nullable, so a user who never set one has no public URL
    at all. Nothing can match ``username=None`` through this lookup, but the
    guard is explicit because an empty-string username would otherwise resolve.
    """
    if not username:
        return None

    user = (
        User.objects
        .select_related("profile")
        .prefetch_related(
            "sports__sport",
            "positions__position",
            "positions__sport",
            # The primary sport's own attribute values ("Preferred foot",
            # "Batting style"). Prefetched whole and filtered to the primary
            # sport in Python — a per-sport queryset here would cost a query
            # and the rows are a handful either way.
            "attributes__attribute",
            "attributes__option",
        )
        .filter(username=username, is_active=True)
        .first()
    )

    if user is None or not user.username:
        return None

    profile = getattr(user, "profile", None)
    if profile is None or not profile.is_public_profile:
        return None

    return user


def get_public_organization(username):
    """The org behind a public profile URL, or None. Mirrors the user twin."""
    if not username:
        return None

    organization = (
        Organization.objects
        .select_related("profile")
        .prefetch_related("sports__sport", "locations")
        # is_suspended mirrors the authenticated lookup in
        # OrganizationService.get_organization — a suspended club must not be
        # reachable by logging out.
        .filter(username=username, is_active=True, is_suspended=False)
        .first()
    )

    if organization is None:
        return None

    profile = getattr(organization, "profile", None)
    if profile is None or not profile.is_public_profile:
        return None

    return organization


# ─────────────────────────────────────────────
# POSTS
# ─────────────────────────────────────────────

def public_posts_page(profile_ref, actor, limit, offset):
    """
    One page of a profile's posts, filtered exactly as the authenticated
    profile list filters them.

    ``profile_ref`` is the ``{"type", "id"}`` dict the shared visibility helper
    expects. ``actor`` is None for an anonymous visitor, which collapses the
    filter to PUBLIC-only — the same code path a signed-in stranger takes when
    they follow nobody.

    Returns ``(queryset, total_count)``.
    """
    queryset = Post.objects.filter(is_deleted=False)

    if profile_ref["type"] == TYPE_USER:
        queryset = queryset.filter(author_user_id=profile_ref["id"])
    else:
        queryset = queryset.filter(author_org_id=profile_ref["id"])

    queryset = queryset.filter(profile_visibility_filter(actor, profile_ref))

    total_count = queryset.count()

    queryset = (
        annotate_is_saved(queryset, actor)
        .select_related("author_user__profile", "author_org", "sport")
        .prefetch_related("media", POST_MENTIONS_PREFETCH)
        .order_by("-created_at")[offset: offset + limit]
    )

    return queryset, total_count


def clamp_page(limit, offset):
    """Parse and bound ?limit / ?offset. Junk falls back to the defaults."""
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = PUBLIC_POSTS_PAGE_SIZE

    try:
        offset = int(offset)
    except (TypeError, ValueError):
        offset = 0

    return (
        min(max(limit, 1), PUBLIC_POSTS_MAX_LIMIT),
        max(offset, 0),
    )


# ─────────────────────────────────────────────
# BUNDLE PARTS
# ─────────────────────────────────────────────

def public_highlights_for(user, actor):
    """
    The clips a visitor may see.

    Delegates to the highlights matrix rather than hardcoding
    ``visibility="everyone"``: for an anonymous actor the matrix already
    resolves to exactly that (not owner, not recruiter, not follower), and
    routing through it means a signed-in scout opening the same URL still gets
    their full rail without a second code path.

    Nothing is recorded — a ``HighlightView`` row is written only by the
    explicit POST /highlights/<id>/view call, which is authenticated. Rendering
    a public profile never touches the view counter.
    """
    return visible_highlights_for(user, actor)


def public_career_entries_for(user):
    """Every entry — verified ones are badged, not filtered."""
    return career_entries_for(user)


def public_achievements_for(user):
    """Every award — same rule as careers."""
    return achievements_for_user(user)


def public_recruitments_for(organization):
    """
    An org's live, publicly-visible listings.

    ACTIVE + PUBLIC only. Deliberately narrower than what a signed-in follower
    sees (FOLLOWERS_ONLY is theirs) and narrower than the share rule (which
    also allows CLOSED — worth forwarding, not worth advertising on a public
    page as though you could still apply).
    """
    return (
        Recruitment.objects
        .filter(
            organization=organization,
            is_deleted=False,
            status=Recruitment.Status.ACTIVE,
            visibility=Recruitment.Visibility.PUBLIC,
        )
        .select_related("organization__profile", "sport")
        .prefetch_related("positions__position", "media")
        .order_by("-published_at", "-created_at")[:PUBLIC_RECRUITMENTS_LIMIT]
    )


def user_profile_ref(user):
    return {"type": TYPE_USER, "id": user.id}


def org_profile_ref(organization):
    return {"type": TYPE_ORGANIZATION, "id": organization.id}


