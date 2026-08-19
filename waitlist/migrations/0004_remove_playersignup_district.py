"""
Step 3 of 3: drop ``district`` and the index on it.

Last so it is the only irreversible-in-practice step, and the only one that has
to wait for the new code to be live. Running 0002 and 0003 leaves a database
both releases can serve; running this one commits to the new one.

Reversing re-creates the column and its index EMPTY — the schema comes back,
the values do not. Reverse 0003 as well to get those back, which is why the
backfill was given a working reverse and why it matches on the label rather
than assuming the column it wrote into is still there.
"""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('waitlist', '0003_backfill_district_into_city'),
    ]

    operations = [
        migrations.RemoveIndex(
            model_name='playersignup',
            name='waitlist_pl_distric_863a3c_idx',
        ),
        migrations.RemoveField(
            model_name='playersignup',
            name='district',
        ),
    ]
