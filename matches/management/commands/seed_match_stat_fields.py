"""
Seed the per-sport match stat catalog.

Re-runnable: every write is a get_or_create, so running it again after adding a
row to the tables below adds only that row and leaves hand-edits in the admin
alone.

Depends on `data_add` having run first (sports and positions). If a named
position is missing, the stat is still created and only the position link is
skipped — a half-seeded database should not cost you the whole catalog.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from matches.models import SportMatchStatField
from sports.models import Sport, SportPosition


INTEGER = SportMatchStatField.ValueType.INTEGER
DECIMAL = SportMatchStatField.ValueType.DECIMAL

# (order, name, short_label, value_type, unit, positions, is_primary)
# An empty positions tuple means "every position" — see the field's comment in
# matches.models.
FOOTBALL_STAT_FIELDS = [
    (1, "Goals", "G", INTEGER, "", (), True),
    (2, "Assists", "A", INTEGER, "", (), True),
    (3, "Shots on target", "SOT", INTEGER, "", (), True),
    (4, "Shots", "SH", INTEGER, "", (), False),
    (5, "Key passes", "KP", INTEGER, "", (), False),
    (6, "Tackles", "TKL", INTEGER, "", (), False),
    (7, "Interceptions", "INT", INTEGER, "", (), False),
    (8, "Saves", "SV", INTEGER, "", ("Goalkeeper",), False),
    (9, "Goals conceded", "GC", INTEGER, "", ("Goalkeeper",), False),
    (10, "Yellow cards", "YC", INTEGER, "", (), False),
    (11, "Red cards", "RC", INTEGER, "", (), False),
    (12, "Distance covered", "DIST", DECIMAL, "km", (), False),
]

# "Balls bowled", not overs, and it is not a naming preference: cricket writes
# overs as 4.3 meaning four overs and three balls, so a decimal column of overs
# sums to nonsense across a season (4.3 + 4.3 = 8.6, not 9.0). We store the
# integer ball count — the one number that adds up — and the UI renders it back
# as overs.
CRICKET_STAT_FIELDS = [
    (1, "Runs scored", "R", INTEGER, "", (), True),
    (2, "Wickets", "W", INTEGER, "", ("Bowler", "All-Rounder"), True),
    (3, "Catches", "CT", INTEGER, "", (), True),
    (4, "Balls faced", "BF", INTEGER, "", ("Batsman", "All-Rounder", "Wicket Keeper"), False),
    (5, "Fours", "4s", INTEGER, "", ("Batsman", "All-Rounder", "Wicket Keeper"), False),
    (6, "Sixes", "6s", INTEGER, "", ("Batsman", "All-Rounder", "Wicket Keeper"), False),
    (7, "Balls bowled", "BB", INTEGER, "", ("Bowler", "All-Rounder"), False),
    (8, "Runs conceded", "RC", INTEGER, "", ("Bowler", "All-Rounder"), False),
    (9, "Maidens", "M", INTEGER, "", ("Bowler", "All-Rounder"), False),
    (10, "Stumpings", "ST", INTEGER, "", ("Wicket Keeper",), False),
    (11, "Run outs", "RO", INTEGER, "", (), False),
]


class Command(BaseCommand):
    help = "Seed per-sport match stat fields for the Match Diary"

    @transaction.atomic
    def handle(self, *args, **kwargs):
        self.stdout.write(self.style.WARNING("Seeding match stat fields..."))

        self._seed_sport("Football", FOOTBALL_STAT_FIELDS)
        self._seed_sport("Cricket", CRICKET_STAT_FIELDS)

        self.stdout.write(self.style.SUCCESS("✅ Match stat fields seeded successfully!"))

    def _seed_sport(self, sport_name, stat_fields):
        sport = Sport.objects.filter(name=sport_name).first()
        if not sport:
            self.stdout.write(
                self.style.WARNING(
                    f"⚠ Sport '{sport_name}' not found — run `data_add` first. Skipping."
                )
            )
            return

        for order, name, short_label, value_type, unit, positions, is_primary in stat_fields:
            field, _ = SportMatchStatField.objects.get_or_create(
                sport=sport,
                name=name,
                defaults={
                    "short_label": short_label,
                    "unit": unit,
                    "value_type": value_type,
                    "is_primary": is_primary,
                    "order": order,
                }
            )

            for position_name in positions:
                position = SportPosition.objects.filter(
                    sport=sport,
                    name=position_name
                ).first()

                if not position:
                    self.stdout.write(
                        self.style.WARNING(
                            f"⚠ {sport_name} position '{position_name}' not found "
                            f"— '{name}' left unlinked."
                        )
                    )
                    continue

                # add() on an m2m is already idempotent, so a re-run is a no-op
                # rather than a duplicate row.
                field.positions.add(position)

        self.stdout.write(self.style.SUCCESS(f"{sport_name} stat fields seeded"))
