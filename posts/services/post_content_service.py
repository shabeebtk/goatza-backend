"""
Parsing of a post's free-text body into the structured rows that hang off it.

Right now that means hashtags; the single entry point views call is
``sync_post_content``, so adding another parsed entity (mentions) is one more
call inside that wrapper and no view has to change.

Everything here is DIFF-BASED and idempotent: syncing a post twice is a no-op,
which is what lets the same code serve create, update and the backfill command.
"""

import re

# Deliberately narrow: letters, digits and underscore only, so a trailing "."
# or "," in prose never becomes part of the tag. Mirrored byte-for-byte by the
# frontend's linkifier in PostCard.tsx — change one, change the other.
HASHTAG_RE = re.compile(r"#([A-Za-z0-9_]{1,50})")
MAX_HASHTAGS_PER_POST = 30


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


def sync_post_content(post) -> None:
    """
    Re-derive every parsed entity for a post from its current content.

    The ONLY function views should call — it is where each new parsed entity
    gets hooked in, so the call sites never grow.
    """
    sync_post_hashtags(post)
