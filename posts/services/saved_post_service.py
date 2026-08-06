"""
Saving a post is per-ACTOR, not per-person: acting as an organization saves to
that org's list, and the same post can be saved independently by a user and by
an org they run.

Everything the read path needs is `annotate_is_saved` — it must be applied to
EVERY queryset that reaches PostListSerializer. A missing annotation renders an
empty bookmark on a post the viewer has actually saved, which reads as data
loss, so the serializer defaults to False rather than raising and the call
sites are the thing to keep honest.
"""

from django.db.models import BooleanField, Exists, OuterRef, Value

from posts.models import SavedPost


def _actor_filter(actor) -> dict | None:
    """The SavedPost column this actor owns, or None if there isn't one."""
    if actor and actor.is_user:
        return {"user": actor.user}
    if actor and actor.is_org:
        return {"org": actor.organization}
    return None


def annotate_is_saved(queryset, actor):
    """
    Add `is_saved` for the CURRENT actor to a post queryset.

    Exists() rather than a join, for the same reason the hashtag search branch
    uses it: a join widens the row set and would force a distinct() that fights
    the keyset paginators.
    """
    saved_by_actor = _actor_filter(actor)

    if saved_by_actor is None:
        # No resolvable actor — still emit the key so the client never has to
        # distinguish "not saved" from "field absent".
        return queryset.annotate(
            is_saved=Value(False, output_field=BooleanField())
        )

    return queryset.annotate(
        is_saved=Exists(
            SavedPost.objects.filter(post=OuterRef("pk"), **saved_by_actor)
        )
    )


def toggle_save(actor, post) -> bool:
    """
    Flip the save state of `post` for `actor`. Returns the NEW state.

    Idempotent per actor thanks to the partial unique constraints: a double-tap
    can only ever delete the one row or create the one row.
    """
    saved_by_actor = _actor_filter(actor)
    if saved_by_actor is None:
        raise ValueError("Invalid actor")

    existing = SavedPost.objects.filter(post=post, **saved_by_actor).first()

    if existing:
        existing.delete()
        return False

    SavedPost.objects.create(post=post, **saved_by_actor)
    return True


def saved_posts_queryset(actor):
    """
    The actor's saved posts, most recently SAVED first.

    Ordered by the save row, not the post: re-saving an old post should put it
    at the top of the list, which is when it became something they wanted back.
    """
    saved_by_actor = _actor_filter(actor)
    if saved_by_actor is None:
        return SavedPost.objects.none()

    return (
        SavedPost.objects
        .filter(post__is_deleted=False, **saved_by_actor)
        .order_by("-created_at", "-id")
    )
