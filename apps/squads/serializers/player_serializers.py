"""
The player's side: the invite link's page and "my squads".

The invite page NEVER carries a player's name or photo — anyone with the link
can open it. The head coach is named (staff are the club's public face) by name
only. "My squads" is the player's own data and carries no notes.
"""

from apps.squads.selectors.squad_selectors import display_name


def _sport(sport):
    return {"id": str(sport.id), "name": sport.name, "icon_name": sport.icon_name}


def _logo(organization):
    profile = getattr(organization, "profile", None)
    return getattr(profile, "logo", "") or ""


def invite_page_payload(squad, head_coach, me):
    organization = squad.organization
    return {
        "squad": {
            "id": str(squad.id),
            "name": squad.name,
            "sport": _sport(squad.sport),
            "level": squad.level,
            "level_label": squad.get_level_display() if squad.level else "",
            "age_group": squad.age_group,
            "season": squad.season,
            "photo_url": squad.photo_url,
            "members_count": squad.members_count,
        },
        "organization": {
            "id": str(organization.id),
            "name": organization.name,
            "username": organization.username,
            "logo": _logo(organization),
            "type": organization.type,
            "is_verified": organization.is_verified,
        },
        "head_coach": (
            {"name": display_name(head_coach.member.user)} if head_coach else None
        ),
        "me": me,
    }


def my_squad_item(row):
    squad = row.squad
    organization = squad.organization
    position = row.position
    return {
        "id": str(row.id),
        "status": row.status,
        "source": row.source,
        "squad": {
            "id": str(squad.id),
            "name": squad.name,
            "sport": _sport(squad.sport),
            "photo_url": squad.photo_url,
            "level_label": squad.get_level_display() if squad.level else "",
            "age_group": squad.age_group,
        },
        "organization": {
            "id": str(organization.id),
            "name": organization.name,
            "username": organization.username,
            "logo": _logo(organization),
            "is_verified": organization.is_verified,
        },
        "position": (
            {"id": str(position.id), "name": position.name} if position else None
        ),
        "jersey_number": row.jersey_number,
        "is_captain": row.is_captain,
        "requested_at": row.requested_at,
        "expires_at": row.expires_at,
        "joined_at": row.joined_at,
    }
