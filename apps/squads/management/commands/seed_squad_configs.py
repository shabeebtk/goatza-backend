"""
Seed how squads behave per sport: one SportSquadConfig row per sport, and the
roster section (SportPosition.group) of each of its positions.

ADDING A SPORT = one entry in SQUAD_CONFIGS below (its config + its position
groups) and a re-run. Zero code: nothing else in the app names a sport. A sport
with no entry still works — squad_config_for hands back the permissive default.

Runs after `data_add` (sports and positions). A missing sport or position is a
warning and the rest carries on: a half-seeded database should not cost you
the whole catalog.

Re-runnable. It writes ONLY the fields listed here (update_or_create on the
config, a plain update of `group` on a position) and never deletes anything.
That also means a re-run puts these fields back to what this file says: an
edit to one of them in the admin lasts until the next run, so a lasting change
belongs here.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.sports.models import Sport, SportPosition
from apps.squads.models import SportSquadConfig


SQUAD_CONFIGS = {
    "Football": {
        "config": {
            "uses_positions": True,
            "uses_jersey_numbers": True,
            "member_noun": "player",
            "member_noun_plural": "players",
            "typical_squad_size": 25,
            "group_order": ["Goalkeepers", "Defenders", "Midfielders", "Forwards"],
        },
        "groups": {
            "Goalkeeper": "Goalkeepers",
            "Defender": "Defenders",
            "Midfielder": "Midfielders",
            "Striker": "Forwards",
            "Left Wing": "Forwards",
            "Right Wing": "Forwards",
        },
    },
    "Cricket": {
        "config": {
            "uses_positions": True,
            "uses_jersey_numbers": False,
            "member_noun": "player",
            "member_noun_plural": "players",
            "typical_squad_size": 16,
            "group_order": ["Batters", "All-rounders", "Wicket-keepers", "Bowlers"],
        },
        "groups": {
            "Batsman": "Batters",
            "All-Rounder": "All-rounders",
            "Wicket Keeper": "Wicket-keepers",
            "Bowler": "Bowlers",
        },
    },
}


class Command(BaseCommand):
    help = "Seed per-sport squad configs and position groups (run after data_add)"

    @transaction.atomic
    def handle(self, *args, **kwargs):
        self.stdout.write(self.style.WARNING("Seeding squad configs..."))

        for sport_name, seed in SQUAD_CONFIGS.items():
            self._seed_sport(sport_name, seed["config"], seed["groups"])

        self.stdout.write(self.style.SUCCESS("✅ Squad configs seeded successfully!"))

    def _seed_sport(self, sport_name, config, groups):
        sport = Sport.objects.filter(name=sport_name).first()
        if not sport:
            self.stdout.write(
                self.style.WARNING(
                    f"⚠ Sport '{sport_name}' not found — run `data_add` first. Skipping."
                )
            )
            return

        _, created = SportSquadConfig.objects.update_or_create(
            sport=sport,
            defaults=config,
        )

        for position_name, group in groups.items():
            updated = SportPosition.objects.filter(
                sport=sport,
                name=position_name,
            ).update(group=group)

            if not updated:
                self.stdout.write(
                    self.style.WARNING(
                        f"⚠ {sport_name} position '{position_name}' not found "
                        f"— no '{group}' group set."
                    )
                )

        verb = "created" if created else "updated"
        self.stdout.write(self.style.SUCCESS(f"{sport_name} squad config {verb}"))
