from django.core.management.base import BaseCommand
from django.db import transaction

from sports.models import (
    Sport,
    SportAttribute,
    SportAttributeOption,
    SportPosition,
)


class Command(BaseCommand):
    help = "Seed sports, attributes, options, and positions"

    @transaction.atomic
    def handle(self, *args, **kwargs):
        self.stdout.write(self.style.WARNING("Seeding sports data..."))

        # =========================
        # ⚽ FOOTBALL
        # =========================
        football, _ = Sport.objects.get_or_create(
            name="Football",
            defaults={"icon_name": "mdi:football"}
        )

        football_positions = [
            "Striker", "Left Wing", "Right Wing",
            "Midfielder", "Defender", "Goalkeeper"
        ]

        for pos in football_positions:
            SportPosition.objects.get_or_create(
                sport=football,
                name=pos
            )

        football_attributes = [
            ("Strong Foot", "select", True, ["Left", "Right", "Both"]),
            ("Preferred Role", "multi_select", False, ["Attacker", "Playmaker", "Finisher", "Defensive", "Keeper"]),
        ]

        for index, (name, dtype, required, options) in enumerate(football_attributes, start=1):
            attr, _ = SportAttribute.objects.get_or_create(
                sport=football,
                name=name,
                defaults={
                    "data_type": dtype,
                    "is_required": required,
                    "display_order": index
                }
            )

            if options:
                for opt in options:
                    SportAttributeOption.objects.get_or_create(
                        attribute=attr,
                        value=opt
                    )

        self.stdout.write(self.style.SUCCESS("Football seeded"))

        # =========================
        # 🏏 CRICKET
        # =========================
        cricket, _ = Sport.objects.get_or_create(
            name="Cricket",
            defaults={"icon_name": "mdi:cricket"}
        )

        cricket_positions = [
            "Batsman", "Bowler", "All-Rounder", "Wicket Keeper"
        ]

        for pos in cricket_positions:
            SportPosition.objects.get_or_create(
                sport=cricket,
                name=pos
            )

        cricket_attributes = [
            ("Batting Style", "select", True, ["Right-hand", "Left-hand"]),
            ("Bowling Type", "select", False, ["Fast", "Medium", "Spin"]),
            ("Bowling Style", "multi_select", False, ["Fast", "Swing", "Seam", "Off Spin", "Leg Spin"]),
        ]

        for index, (name, dtype, required, options) in enumerate(cricket_attributes, start=1):
            attr, _ = SportAttribute.objects.get_or_create(
                sport=cricket,
                name=name,
                defaults={
                    "data_type": dtype,
                    "is_required": required,
                    "display_order": index
                }
            )

            if options:
                for opt in options:
                    SportAttributeOption.objects.get_or_create(
                        attribute=attr,
                        value=opt
                    )

        self.stdout.write(self.style.SUCCESS("Cricket seeded"))

        self.stdout.write(self.style.SUCCESS("✅ All sports data seeded successfully!"))