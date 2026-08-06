"""
Parsing of a post's free-text body into the structured rows that hang off it:
hashtags and mentions. The single entry point views call is
``sync_post_content``, so adding another parsed entity means one more call
inside that wrapper and no view has to change.

Everything here is DIFF-BASED and idempotent: syncing a post twice is a no-op,
which is what lets the same code serve create, update and the backfill command.
"""

import re

# Deliberately narrow: letters, digits and underscore only, so a trailing "."
# or "," in prose never becomes part of the tag. Mirrored byte-for-byte by the
# frontend's linkifier in PostCard.tsx — change one, change the other.
HASHTAG_RE = re.compile(r"#([A-Za-z0-9_]{1,50})")
MAX_HASHTAGS_PER_POST = 30

# Handles differ by actor type: users are [A-Za-z0-9_] (utils.validations
# .validate_username_format), organizations also allow "." (the RegexValidator
# on Organization.username). So the union charset is letters/digits/_/. — but
# a dot may only sit BETWEEN segments, never at the end, or "Great game
# @kochifc." in ordinary prose would capture the full stop and resolve to
# nothing. Both models cap username at 50 chars, so the token cap matches that
# rather than a shorter guess. Mirrored by PostCard.tsx.
MENTION_RE = re.compile(r"@([A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*)")
MAX_MENTION_LENGTH = 50
MAX_MENTIONS_PER_POST = 20


def extract_hashtags(content: str) -> list[str]:
    """Unique, lowercased tag names in first-appearance order, capped."""
    seen, out = set(), []
    for m in HASHTAG_RE.finditer(content or ""):
        tag = m.group(1).lower()
        if tag not in seen:
            seen.add(tag)
            out.append(tag)
        if len(out) >= MAX_HASHTAGS_PER_POST:
            break
    return out


def sync_post_hashtags(post) -> None:
    """
    Diff-sync PostHashtag rows to match the post's current content.
    Idempotent; safe on create and update.
    """
    from posts.models import Hashtag, PostHashtag

    wanted = set(extract_hashtags(post.content))
    existing = {
        ph.hashtag.name: ph
        for ph in PostHashtag.objects.filter(post=post).select_related("hashtag")
    }

    # remove tags no longer present
    to_delete = [ph.id for name, ph in existing.items() if name not in wanted]
    if to_delete:
        PostHashtag.objects.filter(id__in=to_delete).delete()

    # add new tags
    for name in wanted - set(existing):
        hashtag, _ = Hashtag.objects.get_or_create(name=name)
        PostHashtag.objects.get_or_create(post=post, hashtag=hashtag)


# ─────────────────────────────────────────────
# MENTIONS
# ─────────────────────────────────────────────

def extract_mention_usernames(content: str) -> list[str]:
    """
    Unique handles in first-appearance order, capped.

    Case is PRESERVED — the lookup is case-insensitive but the stored username
    is whatever the account actually has, so uniqueness is folded here to stop
    "@Rahul10" and "@rahul10" costing two queries for one person.
    """
    seen, out = set(), []
    for m in MENTION_RE.finditer(content or ""):
        username = m.group(1)
        if len(username) > MAX_MENTION_LENGTH:
            continue
        key = username.lower()
        if key not in seen:
            seen.add(key)
            out.append(username)
        if len(out) >= MAX_MENTIONS_PER_POST:
            break
    return out


def resolve_mention_target(username: str):
    """
    Returns ("user", User) | ("org", Organization) | None.
    Users and organizations have SEPARATE username tables, so the same
    handle can exist in both. Policy: the USER wins; org resolves only when
    no user matches. (Documented product decision — leave this comment.)
    """
    from accounts.models import User
    from organization.models import Organization

    user = User.objects.filter(username__iexact=username, is_active=True).first()
    if user:
        return ("user", user)
    # Organization carries its own is_active flag — a deactivated org is not a
    # mentionable target, same as a deactivated user.
    org = Organization.objects.filter(
        username__iexact=username, is_active=True
    ).first()
    return ("org", org) if org else None


def sync_post_mentions(post) -> list[tuple[str, object]]:
    """
    Diff-sync PostMention rows to the current content.
    Returns ONLY the newly added targets [("user", u) | ("org", o), ...] so
    the caller notifies new mentions on edit without re-notifying old ones.
    """
    from posts.models import PostMention

    # Resolve first: an unknown handle is simply not a mention, and two handles
    # that resolve to the same account collapse to one row.
    wanted_users, wanted_orgs = {}, {}
    for username in extract_mention_usernames(post.content):
        resolved = resolve_mention_target(username)
        if resolved is None:
            continue
        kind, target = resolved
        (wanted_users if kind == "user" else wanted_orgs)[target.id] = target

    existing = list(
        PostMention.objects.filter(post=post)
        .select_related("mentioned_user", "mentioned_org")
    )
    existing_user_ids = {m.mentioned_user_id for m in existing if m.mentioned_user_id}
    existing_org_ids = {m.mentioned_org_id for m in existing if m.mentioned_org_id}

    # remove mentions no longer present
    to_delete = [
        m.id for m in existing
        if (m.mentioned_user_id and m.mentioned_user_id not in wanted_users)
        or (m.mentioned_org_id and m.mentioned_org_id not in wanted_orgs)
    ]
    if to_delete:
        PostMention.objects.filter(id__in=to_delete).delete()

    # add new mentions — the caller only ever notifies what comes back here
    added: list[tuple[str, object]] = []

    for user_id, user in wanted_users.items():
        if user_id in existing_user_ids:
            continue
        _, created = PostMention.objects.get_or_create(post=post, mentioned_user=user)
        if created:
            added.append(("user", user))

    for org_id, org in wanted_orgs.items():
        if org_id in existing_org_ids:
            continue
        _, created = PostMention.objects.get_or_create(post=post, mentioned_org=org)
        if created:
            added.append(("org", org))

    return added


# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────

def sync_post_content(post) -> list[tuple[str, object]]:
    """
    Re-derive every parsed entity for a post from its current content.

    The ONLY function views should call — it is where each new parsed entity
    gets hooked in, so the call sites never grow.

    Returns the NEWLY added mention targets so the caller can notify exactly
    those and nobody who was already mentioned before an edit.
    """
    sync_post_hashtags(post)
    return sync_post_mentions(post)
