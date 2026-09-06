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

from django.db.models import F

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

# Ceiling on either list in the sitemap feed. A sitemap file may hold 50,000
# URLs, so this is nowhere near the format's limit — it is a bound on the
# QUERY, so a table that grows to millions of rows can never turn an hourly
# crawler refresh into a full scan serialized into memory. Splitting into a
# sitemap index is the change to make when this cap starts biting, not raising
# it.
SITEMAP_MAX_ROWS = 5000


# ─────────────────────────────────────────────
# VISIBILITY
# ─────────────────────────────────────────────
# THE definition of "has a public profile", for users and for orgs. Both the
# by-username lookups below and the sitemap feed build on these, so a rule
# added here (a new opt-out, a new moderation state) reaches the shareable page
# and the list of pages we ask Google to crawl in the same edit. Two copies of
# this predicate would eventually disagree, and the way it would show up is a
# hidden profile advertised in a sitemap.

def public_users_queryset():
    """
    Every user whose profile an anonymous visitor may see.

      * ``is_active`` — excludes deactivated, unverified and staff-suspended
        accounts, and a soft-deleted one too: confirming a deletion flips this
        off (accounts.services.account_deletion_service).
      * ``deletion_requested_at`` — redundant with the above today and stated
        anyway, because "not soft-deleted" is a rule of this surface and should
        not depend on another module continuing to set the two together.
      * ``profile__is_public_profile`` — the owner's opt-out. The join also
        drops a user with no profile row at all, which is the same None the
        old explicit check produced.
      * a username — it is nullable, and a user who never set one has no
        public URL to be reached at.
    """
    return (
        User.objects
        .filter(
            is_active=True,
            deletion_requested_at__isnull=True,
            profile__is_public_profile=True,
        )
        .exclude(username__isnull=True)
        .exclude(username="")
    )


def public_organizations_queryset():
    """
    Every organization whose profile an anonymous visitor may see.

    ``is_suspended`` mirrors the authenticated lookup in
    ``OrganizationService.get_organization`` — a suspended club must not be
    reachable by logging out.
    """
    return (
        Organization.objects
        .filter(
            is_active=True,
            is_suspended=False,
            profile__is_public_profile=True,
        )
        .exclude(username="")
    )


# ─────────────────────────────────────────────
# RESOLUTION
# ─────────────────────────────────────────────

def get_public_user(username):
    """
    The user behind a public profile URL, or None.

    Several separate reasons to return None — no such username, deactivated,
    deleted, owner opted out — all of which the view reports as the same 404. A
    visitor must not be able to tell a hidden profile from a deactivated one
    from a typo. The reasons themselves live in ``public_users_queryset``, which
    the sitemap feed reads too so the two can never disagree.
    """
    if not username:
        return None

    return (
        public_users_queryset()
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
        .filter(username=username)
        .first()
    )


def get_public_organization(username):
    """The org behind a public profile URL, or None. Mirrors the user twin."""
    if not username:
        return None

    return (
        public_organizations_queryset()
        .select_related("profile")
        .prefetch_related("sports__sport", "locations")
        .filter(username=username)
        .first()
    )


# ─────────────────────────────────────────────
# SITEMAP FEED
# ─────────────────────────────────────────────
# ``updated_at`` is the PROFILE's, not the User/Organization row's. The account
# row is touched by things a crawler does not care about — SIMPLE_JWT's
# UPDATE_LAST_LOGIN saves it on every login — and a <lastmod> that moves every
# time somebody signs in is a lastmod search engines learn to ignore. The
# profile row moves when the page's content moves.

def public_user_sitemap_rows():
    """
    ``[{"username", "profile_updated_at"}]`` for the sitemap feed, freshest
    first.

    The alias is ``profile_updated_at``, not ``updated_at``: both User and
    Organization already HAVE an ``updated_at`` column, and Django refuses an
    annotation that shadows a real field. The view renames it on the way out.
    """
    return list(
        public_users_queryset()
        .order_by("-profile__updated_at")
        .values("username", profile_updated_at=F("profile__updated_at"))
        [:SITEMAP_MAX_ROWS]
    )


def public_organization_sitemap_rows():
    """The org twin of ``public_user_sitemap_rows``."""
    return list(
        public_organizations_queryset()
        .order_by("-profile__updated_at")
        .values("username", profile_updated_at=F("profile__updated_at"))
        [:SITEMAP_MAX_ROWS]
    )


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


