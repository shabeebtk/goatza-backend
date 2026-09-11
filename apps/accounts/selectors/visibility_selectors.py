"""
The one read-side exclusion for accounts that are no longer on the platform.

``is_active=False`` means a user cannot log in. On its own that does NOT make
them invisible: their name still surfaced in explore, in search and in the
message-target picker, and their posts still rode the feed. This module is the
other half — the part that actually hides the person — and it is deliberately
shaped like ``moderation.selectors.blocked_filters`` so the two read the same
at every call site.

Three states share ``is_active=False`` and all three are hidden by these
helpers, which is correct for every one of them:

  * a user-initiated deletion waiting out its 30 days
  * an unverified signup that never confirmed its OTP
  * a staff suspension

Organizations are untouched. An org is a separate identity with its own
``is_active`` / ``is_suspended`` pair that every org read path already filters
on, and a club does not stop existing because one of its members left — so a
post authored by an ORG stays visible even when the human who published it has
gone.
"""

from django.db.models import Q


def exclude_inactive_authors(queryset, author_field="author_user"):
    """
    Drop rows authored by a deactivated user, keeping every org-authored row.

    ``exclude(author_user__is_active=False)`` alone would be wrong on a
    dual-actor table: an org-authored row has ``author_user`` NULL, the join
    produces NULL rather than False, and ``NOT (NULL = False)`` is NULL — so
    the org's posts would be excluded along with the deactivated users'. The
    explicit "is not null AND is not active" pair below is what keeps them.

    Costs one extra LEFT JOIN condition on an indexed FK. There is no fast path
    here (unlike ``exclude_blocked``): the answer depends on rows in the
    queryset, not on a cheap per-actor set, so the filter always applies.
    """
    return queryset.exclude(
        Q(**{f"{author_field}__isnull": False})
        & Q(**{f"{author_field}__is_active": False})
    )


def active_users(queryset):
    """
    The same rule for a queryset whose rows ARE the users (explore players,
    message-target search, any people list).

    A plain ``filter(is_active=True)`` — no NULL problem to work around,
    because there is no join.
    """
    return queryset.filter(is_active=True)
