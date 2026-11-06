"""
Admin is retired: every "admin" member becomes an "owner".

No behaviour is lost, because every permission list in the app was owner + admin
(see apps/organization/permissions.py). It is a promotion only in name.

joined_at gets its real value here too. The column was added with default=now,
so without this every existing member would read "Joined" today.

REVERSE IS A NO-OP. Nothing records who used to be an admin, and turning owners
back into admins would demote people who were always owners.
"""

from django.db import migrations
from django.db.models import F


def retire_admin_role(apps, schema_editor):
    OrganizationMember = apps.get_model("organization", "OrganizationMember")

    OrganizationMember.objects.filter(role="admin").update(role="owner")
    OrganizationMember.objects.update(joined_at=F("created_at"))


class Migration(migrations.Migration):

    dependencies = [
        ("organization", "0013_organizationmember_added_by_and_more"),
    ]

    operations = [
        migrations.RunPython(retire_admin_role, migrations.RunPython.noop),
    ]
