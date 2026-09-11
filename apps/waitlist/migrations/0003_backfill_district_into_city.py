"""
Step 2 of 3: move the Kerala-only data into the new columns.

Every row written before this release has a ``district`` slug and nothing else
to say where the player is. The display name of that slug is the closest thing
to a city those rows have, so it becomes ``city``, and ``state`` is set to
"Kerala" because that is what the old column meant — it was the only state the
form allowed.

What is NOT set: ``location``, ``country_code``, ``latitude``, ``longitude``.
Guessing a coordinate for "Thrissur" would put a signup somewhere the player
never told us they were, and a real Location row must come from the geocoder,
not from a migration. Those rows stay text-only, exactly like a signup whose
geocoding failed — a shape the service and the serializers already handle.

``other`` is skipped. Its display name is "Other", which is not a city, and
writing it into ``city`` would create rows that look geocoded and are not.
"""

from django.db import migrations

# The slugs the old column allowed, and the label each was shown as. Written
# out rather than read from the model: the choices live in a migration state
# here, not in models.py any more, and a data migration that depends on today's
# code is a data migration that breaks the first time the code moves on.
DISTRICT_LABELS = {
    "thiruvananthapuram": "Thiruvananthapuram",
    "kollam": "Kollam",
    "pathanamthitta": "Pathanamthitta",
    "alappuzha": "Alappuzha",
    "kottayam": "Kottayam",
    "idukki": "Idukki",
    "ernakulam": "Ernakulam",
    "thrissur": "Thrissur",
    "palakkad": "Palakkad",
    "malappuram": "Malappuram",
    "kozhikode": "Kozhikode",
    "wayanad": "Wayanad",
    "kannur": "Kannur",
    "kasaragod": "Kasaragod",
}


def district_to_city(apps, schema_editor):
    """Copy the district's display name into ``city``; state becomes Kerala."""
    PlayerSignup = apps.get_model("waitlist", "PlayerSignup")

    for slug, label in DISTRICT_LABELS.items():
        (
            PlayerSignup.objects
            .filter(district=slug)
            .update(city=label, state="Kerala")
        )


def city_to_district(apps, schema_editor):
    """
    Put the district slug back, and clear the city it was copied into.

    ``state`` is deliberately left as it is. The forward set it to "Kerala",
    which is what these rows already held from the old column default, so
    blanking it here would destroy data the forward never wrote.

    Only rows whose city is exactly one of the fourteen labels are touched. A
    signup created after this release — from anywhere in the world, with a real
    geocoded city — is left alone, because there is no district to put it in.
    """
    PlayerSignup = apps.get_model("waitlist", "PlayerSignup")

    for slug, label in DISTRICT_LABELS.items():
        (
            PlayerSignup.objects
            .filter(city=label, location__isnull=True)
            .update(district=slug, city="")
        )


class Migration(migrations.Migration):

    dependencies = [
        ('waitlist', '0002_playersignup_location_block'),
    ]

    operations = [
        migrations.RunPython(district_to_city, city_to_district),
    ]
