"""
Read queries for career entries.

A career history is a short, fully-public list — anyone who can see the profile
sees every entry — so there is no visibility matrix here, only the ordering rule
and the joins that keep a page of entries at a constant query count.
"""

from django.db.models import DateField, QuerySet, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from careers.models import CareerEntry


def career_entries_for(user) -> QuerySet:
    """
    ``user``'s career entries in profile order:

      1. current entries first (``is_current`` DESC)
      2. then most recently finished — an open-ended entry is treated as ending
         today, so a still-running spell without ``is_current`` still sorts
         above one that ended last year
      3. then most recent start, which breaks ties between entries that share
         an end date (a loan and the parent contract, say)

    ``COALESCE(end_date, today)`` is computed in SQL, not in Python, so the sort
    survives pagination and the DB can use the ``(user, start_date)`` index for
    the scan.

    Joins the organization (and its profile, for the logo) and the sport, and
    prefetches positions — three queries for any number of entries.
    """
    if user is None:
        return CareerEntry.objects.none()

    return (
        CareerEntry.objects
        .filter(user=user)
        .select_related("organization", "organization__profile", "sport")
        .prefetch_related("positions")
        .annotate(
            effective_end=Coalesce(
                "end_date",
                # A date bound, not NOW(): end_date is a DateField, and mixing
                # in a timestamp would make the comparison type-unstable.
                Value(timezone.localdate(), output_field=DateField()),
                output_field=DateField(),
            )
        )
        # Meta.ordering is newest-start-first; this replaces it wholesale.
        .order_by("-is_current", "-effective_end", "-start_date")
    )


def career_entry_count_for(user) -> int:
    """How many entries the profile header shows next to "Career"."""
    if user is None:
        return 0

    return CareerEntry.objects.filter(user=user).count()


def _org_review_queryset(organization, statuses, ordering) -> QuerySet:
    """
    Shared base for both halves of an org's review screen.

    Joins the claimant's profile and the sport and prefetches positions, so a
    page costs three queries however many rows it holds.
    """
    if organization is None:
        return CareerEntry.objects.none()

    return (
        CareerEntry.objects
        .filter(organization=organization, verification_status__in=statuses)
        .select_related("user", "user__profile", "sport")
        .prefetch_related("positions")
        .order_by(*ordering)
    )


def pending_verification_requests_for(organization) -> QuerySet:
    """
    Entries waiting on ``organization``'s decision — the "Requests" tab.

    Oldest first: this is a work queue, and the person who has been waiting
    longest should be dealt with first. (The history below is the opposite —
    there, the most recent decision is the interesting one.)

    Nothing needs cleaning up when a player re-tags an entry to a different
    club: this reads through the ``organization`` FK, so the entry leaves the
    old club's queue the moment the link moves.
    """
    return _org_review_queryset(
        organization,
        [CareerEntry.VerificationStatus.PENDING],
        ["created_at"],
    )


def decided_verification_requests_for(organization) -> QuerySet:
    """
    Entries ``organization`` has already ruled on — the "History" tab.

    Most recently touched first. A club can revisit any of these (verify what
    it once rejected, or withdraw a confirmation), which is why history is a
    first-class list rather than a write-only log.
    """
    return _org_review_queryset(
        organization,
        [
            CareerEntry.VerificationStatus.VERIFIED,
            CareerEntry.VerificationStatus.REJECTED,
        ],
        ["-updated_at"],
    )
