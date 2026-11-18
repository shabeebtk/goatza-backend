"""
The "school" organization type is retired: every school page becomes a team.

An organization is a club, a team or an academy. A school or college team is a
team, so nothing about the page changes except the label it carries.

The data step runs BEFORE the AlterField, so no row is left holding a value the
new choices do not list.

REVERSE IS A NO-OP. Nothing records which teams used to be schools, and turning
teams back into schools would relabel pages that were always teams.
"""

from django.db import migrations, models


def school_to_team(apps, schema_editor):
    Organization = apps.get_model("organization", "Organization")

    Organization.objects.filter(type="school").update(type="team")


class Migration(migrations.Migration):

    dependencies = [
        ("organization", "0014_retire_admin_role"),
    ]

    operations = [
        migrations.RunPython(school_to_team, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="organization",
            name="type",
            field=models.CharField(
                choices=[("club", "Club"), ("team", "Team"), ("academy", "Academy")],
                max_length=20,
            ),
        ),
    ]
