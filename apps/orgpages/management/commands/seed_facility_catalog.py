"""
Seed the facilities catalog (orgpages.FacilityItem).

ADDING A FACILITY = one entry in CATALOG below and a re-run. Zero code: the
editor and the public page read the catalog from the table.

Re-runnable. It writes ONLY the fields listed here (label, group, order, and
is_active=True) through update_or_create on the key, and never deletes a row.
That also means a re-run puts these fields back to what this file says: an
edit to one of them in the admin lasts until the next run, so a lasting change
belongs here. An item switched off in the admin that is still listed here is
switched back on by a re-run; to retire one for good, remove it from this file
AND switch it off.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.orgpages.models import FacilityItem

G = FacilityItem.Group

# group → [(key, label)], in display order.
CATALOG = {
    G.PLAYING_AREA: [
        ("natural_grass", "Natural grass pitch"),
        ("artificial_turf", "Artificial turf"),
        ("indoor_court", "Indoor court"),
        ("futsal_court", "Futsal court"),
        ("floodlights", "Floodlights"),
    ],
    G.TRAINING: [
        ("gym", "Gym"),
        ("goalkeeping_area", "Goalkeeping area"),
        ("video_analysis", "Video analysis"),
        ("classroom", "Classroom"),
    ],
    G.COMFORT: [
        ("changing_rooms", "Changing rooms"),
        ("drinking_water", "Drinking water"),
        ("washrooms", "Washrooms"),
        ("parking", "Parking"),
        ("seating", "Seating"),
    ],
    G.CARE_TRAVEL: [
        ("first_aid", "First aid"),
        ("physio", "Physio"),
        ("pickup_drop", "Pick-up & drop"),
        ("hostel", "Hostel"),
        ("canteen", "Canteen"),
    ],
}


class Command(BaseCommand):
    help = "Seed the facilities catalog for organization pages (idempotent)"

    @transaction.atomic
    def handle(self, *args, **kwargs):
        created = updated = 0

        for group, items in CATALOG.items():
            for order, (key, label) in enumerate(items):
                _, was_created = FacilityItem.objects.update_or_create(
                    key=key,
                    defaults={
                        "label": label,
                        "group": group,
                        "order": order,
                        "is_active": True,
                    },
                )
                if was_created:
                    created += 1
                else:
                    updated += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Facility catalog seeded: {created} created, {updated} updated."
            )
        )
