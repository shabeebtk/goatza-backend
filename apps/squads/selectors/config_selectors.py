"""
How squads behave for a sport — read from seeded data, never from code.

A sport with no SportSquadConfig row gets the permissive default below, so a
sport added to the catalog works with squads before anyone seeds it: positions
on (whatever positions the sport has), jersey numbers off, "player"/"players",
no section order.
"""

from apps.sports.models import SportPosition
from apps.squads.models import SportSquadConfig


DEFAULT_SQUAD_CONFIG = {
    "uses_positions": True,
    "uses_jersey_numbers": False,
    "member_noun": "player",
    "member_noun_plural": "players",
    "group_order": [],
    "typical_squad_size": None,
}


def squad_config_for(sport, *, with_positions=True) -> dict:
    """
    ``{uses_positions, uses_jersey_numbers, member_noun, member_noun_plural,
    group_order, typical_squad_size, positions: [{id, name, group}]}`` for
    ``sport`` (a Sport instance).

    ``positions`` are the sport's positions by name, whatever ``uses_positions``
    says — the client decides whether to offer them. ``with_positions=False``
    leaves the key out and costs no query: the showcase only needs the flags.
    The reverse one-to-one is read through the accessor, so a caller that
    ``select_related("squad_config")`` pays no query for it (a miss is cached
    too).
    """
    try:
        config = sport.squad_config
    except SportSquadConfig.DoesNotExist:
        config = None

    if config is None:
        data = dict(DEFAULT_SQUAD_CONFIG, group_order=[])
    else:
        data = {
            "uses_positions": config.uses_positions,
            "uses_jersey_numbers": config.uses_jersey_numbers,
            "member_noun": config.member_noun,
            "member_noun_plural": config.member_noun_plural,
            # A copy: the caller may sort or append without touching the row.
            "group_order": list(config.group_order or []),
            "typical_squad_size": config.typical_squad_size,
        }

    if not with_positions:
        return data

    data["positions"] = [
        {"id": str(position["id"]), "name": position["name"], "group": position["group"]}
        for position in (
            SportPosition.objects
            .filter(sport=sport)
            .order_by("name")
            .values("id", "name", "group")
        )
    ]

    return data
