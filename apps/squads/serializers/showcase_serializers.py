"""
The showcase payloads — what everyone else sees of a club's squads.

AN ALLOW-LIST, like the public profile serializers: every key is written out
here, and nothing is a model field passed through. NEVER on any of these: age,
birthdate, location, the club's notes, how a player joined (``source``), or
anything about requests and invites.

``anonymous=True`` is the logged-out route, where a minor's photo is withheld
(``minor_photo_allowed``) — for players and staff alike. Signed in, photos are
shown as everywhere else in the app.
"""

from apps.accounts.serializers.public_profile_serializers import minor_photo_allowed
from apps.squads.selectors.config_selectors import squad_config_for
from apps.squads.selectors.squad_selectors import display_name, group_members

PREVIEW_MEMBERS = 4
OTHER_SQUADS_MAX = 6


def _photo(user, anonymous):
    profile = getattr(user, "profile", None)
    photo = getattr(profile, "profile_photo", "") or ""
    # Not ``is_minor`` alone: minor_photo_allowed is where a guardian's consent
    # to photos will live. ``is_minor`` answers True for an unknown age.
    if anonymous and getattr(user, "is_minor", True) and not minor_photo_allowed(user):
        return ""
    return photo


def _sport(sport):
    return {"id": str(sport.id), "name": sport.name, "icon_name": sport.icon_name}


def organization_payload(organization):
    profile = getattr(organization, "profile", None)
    return {
        "id": str(organization.id),
        "name": organization.name,
        "username": organization.username,
        "logo": getattr(profile, "logo", "") or "",
        "is_verified": organization.is_verified,
        "type": organization.type,
    }


def _staff_role_label(staff):
    if staff.role == staff.Role.OTHER and staff.title:
        return staff.title
    return staff.get_role_display()


def _head_coach_name(staff_rows):
    head = next(
        (staff for staff in staff_rows if staff.role == staff.Role.HEAD_COACH), None
    )
    return display_name(head.member.user) if head else None


def showcase_list_payload(organization, squads, *, anonymous):
    """``{organization, squads: [...]}`` for ``visible_squads``' result."""
    return {
        "organization": organization_payload(organization),
        "squads": [
            {
                "id": str(item["squad"].id),
                "name": item["squad"].name,
                "sport": _sport(item["squad"].sport),
                "level_label": (
                    item["squad"].get_level_display() if item["squad"].level else ""
                ),
                "age_group": item["squad"].age_group,
                "season": item["squad"].season,
                "photo_url": item["squad"].photo_url,
                # Everyone who counts for this viewer, private profiles
                # included: the same number as the squad page's counts.players.
                "members_count": len(item["players"]),
                # ``staff`` is already only the people this viewer may see, so
                # an unlisted head coach is never named.
                "head_coach_name": _head_coach_name(item["staff"]),
                "preview_members": [
                    {
                        "name": display_name(row.user),
                        "photo": _photo(row.user, anonymous),
                    }
                    for row in item["shown"][:PREVIEW_MEMBERS]
                ],
            }
            for item in squads
        ],
    }


def _member(row, anonymous):
    position = row.position
    return {
        "id": str(row.id),
        "name": display_name(row.user),
        "username": row.user.username or "",
        "photo": _photo(row.user, anonymous),
        "position": (
            {"id": str(position.id), "name": position.name} if position else None
        ),
        "jersey_number": row.jersey_number,
        "is_captain": row.is_captain,
    }


def showcase_squad_payload(organization, entry, others, *, anonymous):
    """One squad page. ``entry`` / ``others`` come from ``showcase_squad``."""
    squad = entry["squad"]
    config = squad_config_for(squad.sport, with_positions=False)

    groups = []
    for group in group_members(entry["players"], config["group_order"]):
        shown = [row for row in group["members"] if row.is_listed]
        groups.append(
            {
                "name": group["name"],
                "count": len(group["members"]),
                "hidden_count": len(group["members"]) - len(shown),
                "members": [_member(row, anonymous) for row in shown],
            }
        )

    captain = next((row for row in entry["shown"] if row.is_captain), None)

    return {
        "organization": organization_payload(organization),
        "squad": {
            "id": str(squad.id),
            "name": squad.name,
            "sport": _sport(squad.sport),
            "level": squad.level,
            "level_label": squad.get_level_display() if squad.level else "",
            "age_group": squad.age_group,
            "season": squad.season,
            "description": squad.description,
            "photo_url": squad.photo_url,
        },
        "config": {
            "uses_positions": config["uses_positions"],
            "uses_jersey_numbers": config["uses_jersey_numbers"],
            "member_noun": config["member_noun"],
            "member_noun_plural": config["member_noun_plural"],
        },
        "staff": [
            {
                "name": display_name(staff.member.user),
                "username": staff.member.user.username or "",
                "photo": _photo(staff.member.user, anonymous),
                "role_label": _staff_role_label(staff),
            }
            for staff in entry["staff"]
        ],
        "counts": {
            "players": len(entry["players"]),
            "staff": len(entry["staff"]),
        },
        "captain_member_id": str(captain.id) if captain else None,
        "groups": groups,
        "other_squads": [
            {
                "id": str(item["squad"].id),
                "name": item["squad"].name,
                "sport": _sport(item["squad"].sport),
                # The same count the list card and that squad's page show.
                "members_count": len(item["players"]),
            }
            for item in others[:OTHER_SQUADS_MAX]
        ],
    }
