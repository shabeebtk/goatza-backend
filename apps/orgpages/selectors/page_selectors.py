"""
What everyone else sees of an organization's page — THE rules, in one place
for both surfaces: the signed-in showcase (``/page/showcase/<org_username>``)
and the anonymous page (``/public/organization/<username>/page``).

WHO CAN SEE IT (``page_organization``, the squads showcase's own rule): the org
is active and not suspended; an anonymous viewer also needs the org's
public-profile setting (``public_organizations_queryset``); a block in either
direction with a signed-in viewer is a miss. Every miss is one 404.

THE CLUB'S OWN MEMBERS GET NO EXCEPTION: a switched-off section stays hidden
here. The editor endpoints are their way in.

WHAT IT SAYS (``page_for``):
  * ``sections`` is the switch state; ``order`` is the type's order with the
    switched-off keys left out and "posts" always last.
  * A switched-off section's list is EMPTY here — never sent and hidden by the
    client. This payload's own lists are also empty when there is nothing;
    squads, locations, trials and posts come from their own endpoints, so the
    client drops those when they are empty.
  * ``contact`` carries a number or an email only while its switch is on.
  * ``facts`` is null when nothing is filled.
  * Players: the squads privacy rules exactly. Anonymous → only users in
    ``public_users_queryset()``, no minor's photo (``minor_photo_allowed``).
    Signed in → nobody in a block with the viewer. A deactivated or
    deletion-requested account never shows; nor does a row whose career entry
    is no longer verified at this org.
  * Member coaches: the same photo rule, and a ``username`` only when this
    viewer could open the profile.
  * NEVER here: enquiries, notes, or any contact detail beyond the
    switched-on contact block.
"""

from django.db.models import Exists, OuterRef

from apps.accounts.serializers.public_profile_serializers import minor_photo_allowed
from apps.careers.models import CareerEntry
from apps.moderation.selectors.blocked_filters import blocked_id_sets
from apps.organization.models import OrganizationLocation
from apps.orgpages import constants as C
from apps.orgpages.models import (
    FacilityItem,
    FacilityPhoto,
    FeaturedPlayer,
    LocationFacilities,
)
from apps.orgpages.selectors.editor_selectors import org_programs
from apps.orgpages.selectors.player_selectors import (
    VERIFIED,
    position_name,
    primary_position_names,
    years_label,
)
from apps.orgpages.serializers.page_serializers import (
    coach_role_label,
    location_mini,
    program_payload,
)
from apps.orgpages.services.coach_service import org_coaches
from apps.orgpages.services.guards import page_settings_for
from apps.squads.selectors.showcase_selectors import showcase_organization
from core.selectors.public_profile_selectors import public_users_queryset


GROUP_ORDER = {group: index for index, group in enumerate(FacilityItem.Group.values)}


def page_organization(username, actor):
    """The org whose page this viewer may see, or None — the squads showcase rule."""
    return showcase_organization(username, actor)


# ---------------------------------------------------------------------
# people
# ---------------------------------------------------------------------

def _photo(user, anonymous):
    profile = getattr(user, "profile", None)
    photo = getattr(profile, "profile_photo", "") or ""
    # Not ``is_minor`` alone: minor_photo_allowed is where a guardian's consent
    # to photos will live. ``is_minor`` answers True for an unknown age.
    if anonymous and getattr(user, "is_minor", True) and not minor_photo_allowed(user):
        return ""
    return photo


def _name(user):
    profile = getattr(user, "profile", None)
    return getattr(profile, "name", "") or user.username or ""


def _live_account(user):
    return user.is_active and user.deletion_requested_at is None


def _public_user_ids(user_ids):
    """The ones an anonymous visitor may open, in one query."""
    if not user_ids:
        return set()
    return set(
        public_users_queryset().filter(id__in=user_ids).values_list("id", flat=True)
    )


# ---------------------------------------------------------------------
# sections
# ---------------------------------------------------------------------

def contact_block(page):
    return {
        "enquiry_form": page.enquiry_form_enabled,
        "whatsapp": page.whatsapp_number if page.whatsapp_enabled and page.whatsapp_number else None,
        "call": page.call_number if page.call_enabled and page.call_number else None,
        "email": page.contact_email if page.email_enabled and page.contact_email else None,
    }


def facts_block(page):
    """The facts, or None when nothing is filled."""
    links = {
        "instagram": page.instagram_url or None,
        "youtube": page.youtube_url or None,
        "facebook": page.facebook_url or None,
    }
    filled = any(
        (
            page.founded_year is not None,
            page.age_min is not None,
            page.age_max is not None,
            bool(page.coaching_languages),
            bool(page.training_days),
            bool(page.student_range),
            any(links.values()),
        )
    )
    if not filled:
        return None
    return {
        "founded_year": page.founded_year,
        "age_min": page.age_min,
        "age_max": page.age_max,
        "coaching_languages": list(page.coaching_languages or []),
        "training_days": list(page.training_days or []),
        "student_range": page.student_range or None,
        "student_range_label": (
            page.get_student_range_display() if page.student_range else None
        ),
        "links": links,
    }


def section_order(organization, page):
    order = C.SECTION_ORDER_BY_TYPE.get(organization.type, C.CLUB_ORDER)
    return [key for key in order if page.section_enabled(key)]


def facilities_block(organization):
    """Only branches that have something: an active item, an own word, or a photo."""
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

    result = []
    for location in locations:
        row = facilities.get(location.id)
        items = (
            sorted(
                (item for item in row.items.all() if item.is_active),
                key=lambda item: (GROUP_ORDER.get(item.group, 99), item.order, item.label),
            )
            if row else []
        )
        custom = list(row.custom_items or []) if row else []
        branch_photos = photos.get(location.id, [])
        if not (items or custom or branch_photos):
            continue
        result.append(
            {
                "location": location_mini(location),
                "items": [
                    {"key": item.key, "label": item.label, "group": item.group}
                    for item in items
                ],
                "custom_items": custom,
                "photos": [
                    {"id": str(photo.id), "url": photo.photo_url}
                    for photo in branch_photos
                ],
            }
        )
    return result


def coaches_block(organization, actor, blocked_user_ids):
    """
    Visible coaches: every non-member coach, and every member coach whose
    member is ACTIVE with a live account and no block with the viewer.
    """
    anonymous = actor is None
    coaches = list(
        org_coaches(organization)
        .filter(is_visible=True)
        .select_related("member__user__profile")
        .order_by("order", "created_at")
    )

    member_user_ids = [c.member.user_id for c in coaches if c.member_id]
    openable = _public_user_ids(member_user_ids) if anonymous else None

    result = []
    for coach in coaches:
        if coach.member_id:
            user = coach.member.user
            if not _live_account(user) or user.id in blocked_user_ids:
                continue
            can_open = (user.id in openable) if anonymous else bool(user.username)
            result.append(
                {
                    "id": str(coach.id),
                    "name": _name(user),
                    "username": user.username if can_open else None,
                    "photo": _photo(user, anonymous),
                    "role_label": coach_role_label(coach),
                    "licence": coach.licence,
                    "years_coaching": coach.years_coaching,
                }
            )
        else:
            result.append(
                {
                    "id": str(coach.id),
                    "name": coach.name,
                    "username": None,
                    "photo": coach.photo_url,
                    "role_label": coach_role_label(coach),
                    "licence": coach.licence,
                    "years_coaching": coach.years_coaching,
                }
            )
    return result


def now_at_names(organization, user_ids):
    """
    ``{user_id: name}``: each player's most recent CURRENT, VERIFIED career
    entry at ANOTHER organization — that org's name, else the entry's own.
    One query.
    """
    if not user_ids:
        return {}
    entries = (
        CareerEntry.objects
        .filter(
            user_id__in=list(user_ids),
            is_current=True,
            verification_status=VERIFIED,
            organization__isnull=False,
        )
        .exclude(organization=organization)
        .select_related("organization")
        .order_by("user_id", "-start_date", "-created_at")
        .distinct("user_id")
    )
    return {
        entry.user_id: (
            entry.organization.name if entry.organization else entry.organization_name
        )
        for entry in entries
    }


def players_block(organization, actor, blocked_user_ids):
    anonymous = actor is None
    rows = (
        FeaturedPlayer.objects
        .filter(
            organization=organization,
            career_entry__organization=organization,
            career_entry__verification_status=VERIFIED,
            user__is_active=True,
            user__deletion_requested_at__isnull=True,
        )
        .select_related("user", "user__profile", "career_entry")
        .prefetch_related("career_entry__positions")
        .order_by("order", "created_at")
    )
    if anonymous:
        rows = rows.filter(
            Exists(public_users_queryset().filter(id=OuterRef("user_id")))
        )
    elif blocked_user_ids:
        rows = rows.exclude(user_id__in=blocked_user_ids)

    rows = list(rows)
    primary_positions = primary_position_names([row.career_entry for row in rows])
    now_at = now_at_names(organization, {row.user_id for row in rows})

    return [
        {
            "username": row.user.username or "",
            "name": _name(row.user),
            "photo": _photo(row.user, anonymous),
            "position": position_name(row.career_entry, primary_positions) or None,
            "years_label": years_label(row.career_entry),
            # The academy's own line wins: with a highlight, the client shows
            # it where "now at" would go, so now_at is not sent.
            "now_at": None if row.highlight else now_at.get(row.user_id),
            "highlight": row.highlight or None,
        }
        for row in rows
    ]


def page_for(organization, actor):
    """The page payload for this viewer. ``actor=None`` is the anonymous page."""
    page = page_settings_for(organization)
    blocked_user_ids = set()
    if actor is not None:
        blocked_user_ids, _ = blocked_id_sets(actor)

    switches = page.section_switches()

    return {
        "organization": {
            "id": str(organization.id),
            "username": organization.username,
            "name": organization.name,
            "type": organization.type,
        },
        "contact": contact_block(page),
        "order": section_order(organization, page),
        "sections": switches,
        "facts": facts_block(page),
        "programs": (
            [program_payload(program) for program in org_programs(organization)]
            if switches["programs"] else []
        ),
        "facilities": facilities_block(organization) if switches["facilities"] else [],
        "coaches": (
            coaches_block(organization, actor, blocked_user_ids)
            if switches["coaches"] else []
        ),
        "players": (
            players_block(organization, actor, blocked_user_ids)
            if switches["players"] else []
        ),
    }
