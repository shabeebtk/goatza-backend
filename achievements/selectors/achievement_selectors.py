"""
Read queries for achievements.

Like a career history, an achievement list is short and fully public — anyone
who can see the profile sees every award — so there is no visibility matrix
here, only the joins that keep a page of cards at a constant query count.

The ordering rule needs no annotation the way careers does: pinned-first, then
newest achieved, then newest created is exactly ``Meta.ordering``, and it is
already backed by the ``(user, is_pinned)`` and ``(user, achieved_date)``
indexes.
"""

from django.db.models import QuerySet

from achievements.models import Achievement


def list_for_user(user) -> QuerySet:
    """
    ``user``'s achievements in profile order — pinned first, then most recently
    achieved, then most recently added.

    Joins the sport, the issuing org (and its profile, for the logo) and the
    linked career entry, so the cards render in one query however many there
    are. Deliberately inherits ``Meta.ordering`` rather than restating it: the
    model's order IS the profile order here, unlike careers where the
    "still running" rule forces an annotation.
    """
    if user is None:
        return Achievement.objects.none()

    return (
        Achievement.objects
        .filter(user=user)
        .select_related(
            "sport",
            "awarded_by",
            "awarded_by__profile",
            "career_entry",
        )
    )


def get_by_id(achievement_id) -> Achievement | None:
    """
    One achievement with the same joins a list row gets, or None.

    Backs the detail endpoint — a shared link or a notification deep link — so
    the single-card response is byte-identical to the row the profile renders.
    """
    if not achievement_id:
        return None

    return (
        Achievement.objects
        .select_related(
            "sport",
            "awarded_by",
            "awarded_by__profile",
            "career_entry",
        )
        .filter(id=achievement_id)
        .first()
    )


def count_for_user(user) -> int:
    """How many awards the profile header shows next to "Achievements"."""
    if user is None:
        return 0

    return Achievement.objects.filter(user=user).count()


def _org_review_queryset(organization, statuses, ordering) -> QuerySet:
    """
    Shared base for both halves of an org's review screen.

    Joins the claimant's profile and the sport, so a page costs two queries
    however many rows it holds.
    """
    if organization is None:
        return Achievement.objects.none()

    return (
        Achievement.objects
        .filter(awarded_by=organization, verification_status__in=statuses)
        .select_related("user", "user__profile", "sport")
        .order_by(*ordering)
    )


def pending_verification_requests_for(organization) -> QuerySet:
    """
    Achievements waiting on ``organization``'s decision — the "Requests" tab.

    Oldest first: this is a work queue, and the person who has been waiting
    longest should be dealt with first. (The history below is the opposite —
    there, the most recent decision is the interesting one.)

    Overrides ``Meta.ordering`` wholesale — the owner's pin choice is a profile
    display preference and has no business reordering somebody else's work
    queue.

    Nothing needs cleaning up when an owner re-credits an award to a different
    org: this reads through the ``awarded_by`` FK, so the row leaves the old
    org's queue the moment the link moves.
    """
    return _org_review_queryset(
        organization,
        [Achievement.VerificationStatus.PENDING],
        ["created_at"],
    )


def decided_verification_requests_for(organization) -> QuerySet:
    """
    Achievements ``organization`` has already ruled on — the "History" tab.

    Most recently touched first. An org can revisit any of these (verify what
    it once rejected, or withdraw a confirmation), which is why history is a
    first-class list rather than a write-only log.
    """
    return _org_review_queryset(
        organization,
        [
            Achievement.VerificationStatus.VERIFIED,
            Achievement.VerificationStatus.REJECTED,
        ],
        ["-updated_at"],
    )
