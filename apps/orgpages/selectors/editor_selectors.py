"""
What the club's editor reads. No privacy filtering beyond the relation-lookup
rule: this is the club looking at its own page, behind ``page.manage`` (the hub
itself is ``members.view``).

``page_status`` is the one place the per-section numbers are counted: the hub
shows them and readiness (``readiness_selectors``) scores them.
"""

from django.db.models import Count, Q

from apps.organization.models import OrganizationLocation, OrganizationMember
from apps.orgpages.models import (
    Coach,
    FacilityItem,
    FacilityPhoto,
    FeaturedPlayer,
    LocationFacilities,
    Program,
)
from apps.orgpages.services.coach_service import org_coaches
from apps.posts.models import Post
from apps.recruitments.models import Recruitment
from apps.recruitments.trial_window import trial_not_over_q
from apps.squads.models import Squad


def open_public_trials(organization):
    """
    The org's live, public, not-yet-over recruitments — the same rule as the
    public bundle's ``public_recruitments_for``, without its page cap.
    """
    return (
        Recruitment.objects
        .filter(
            organization=organization,
            is_deleted=False,
            status=Recruitment.Status.ACTIVE,
            visibility=Recruitment.Visibility.PUBLIC,
        )
        .filter(trial_not_over_q())
    )


def facility_counts(organization):
    """
    ``{"items": n, "photos": n, "max_items_on_a_branch": n, "max_photos_on_a_branch": n}``
    over every branch. Items are active catalog items plus the club's own words.
    """
    rows = list(
        LocationFacilities.objects
        .filter(location__organization=organization)
        .annotate(
            active_items=Count("items", filter=Q(items__is_active=True), distinct=True)
        )
        .values_list("location_id", "active_items", "custom_items")
    )
    items_by_branch = {
        location_id: active + len(custom or [])
        for location_id, active, custom in rows
    }

    photos_by_branch = dict(
        FacilityPhoto.objects
        .filter(location__organization=organization)
        .values("location_id")
        .annotate(n=Count("id"))
        .values_list("location_id", "n")
    )

    return {
        "items": sum(items_by_branch.values()),
        "photos": sum(photos_by_branch.values()),
        "max_items_on_a_branch": max(items_by_branch.values(), default=0),
        "max_photos_on_a_branch": max(photos_by_branch.values(), default=0),
    }


def page_status(organization):
    """Every number the hub and readiness need, in one pass of small counts."""
    profile = getattr(organization, "profile", None)
    description = (getattr(profile, "description", "") or "").strip()

    return {
        "description_length": len(description),
        "has_logo": bool(getattr(profile, "logo", "")),
        "has_cover": bool(getattr(profile, "cover_image", "")),
        "programs": Program.objects.filter(organization=organization).count(),
        "facilities": facility_counts(organization),
        "coaches": org_coaches(organization).filter(is_visible=True).count(),
        "players": FeaturedPlayer.objects.filter(organization=organization).count(),
        "squads": Squad.objects.filter(
            organization=organization, is_deleted=False, is_public=True
        ).count(),
        "locations": OrganizationLocation.objects.filter(
            organization=organization
        ).count(),
        "trials": open_public_trials(organization).count(),
        "posts": Post.objects.filter(
            author_org=organization, is_deleted=False
        ).count(),
    }


def section_status(page, status):
    """Per section: its switch and its number(s) — the hub's ``sections``."""
    switches = page.section_switches()
    facilities = status["facilities"]

    def entry(key, **numbers):
        return {"enabled": switches[key], "switchable": True, **numbers}

    return {
        "about": entry("about", filled=status["description_length"] > 0),
        "programs": entry("programs", count=status["programs"]),
        "facilities": entry(
            "facilities", items=facilities["items"], photos=facilities["photos"]
        ),
        "coaches": entry("coaches", count=status["coaches"]),
        "players": entry("players", count=status["players"]),
        "squads": entry("squads", count=status["squads"]),
        "locations": entry("locations", count=status["locations"]),
        "trials": entry("trials", count=status["trials"]),
        "posts": {"enabled": True, "switchable": False, "count": status["posts"]},
    }


def org_programs(organization):
    return list(
        Program.objects.filter(organization=organization)
        .select_related("sport", "location")
        .order_by("order", "created_at")
    )


def editor_facilities(organization):
    """``(catalog_items, [{location, item_keys, custom_items, photos}])`` for every branch."""
    catalog = list(
        FacilityItem.objects.filter(is_active=True).order_by("group", "order", "label")
    )

    locations = list(
        OrganizationLocation.objects.filter(organization=organization)
        .order_by("-is_primary", "created_at")
    )
    facilities = {
        row.location_id: row
        for row in (
            LocationFacilities.objects
            .filter(location__organization=organization)
            .prefetch_related("items")
        )
    }
    photos = {}
    for photo in (
        FacilityPhoto.objects
        .filter(location__organization=organization)
        .order_by("order", "created_at")
    ):
        photos.setdefault(photo.location_id, []).append(photo)

    branches = []
    for location in locations:
        row = facilities.get(location.id)
        branches.append(
            {
                "location": location,
                "item_keys": sorted(
                    item.key for item in row.items.all() if item.is_active
                ) if row else [],
                "custom_items": list(row.custom_items or []) if row else [],
                "photos": photos.get(location.id, []),
            }
        )
    return catalog, branches


def editor_coaches(organization):
    """``(coaches, candidates)``: the shown coaches in order, and members not added yet."""
    coaches = list(
        org_coaches(organization)
        .select_related("member__user__profile")
        .order_by("order", "created_at")
    )

    added = Coach.objects.filter(
        organization=organization, member__isnull=False
    ).values("member_id")
    candidates = list(
        OrganizationMember.objects
        .filter(
            organization=organization,
            user__is_active=True,
            user__deletion_requested_at__isnull=True,
        )
        .exclude(id__in=added)
        .select_related("user__profile")
        .order_by("joined_at")
    )
    return coaches, candidates
