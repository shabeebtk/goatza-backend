"""
Step 1 of 3: add the location block. Nothing is read or removed here.

Split from the removal on purpose. This migration only widens the table, so it
is safe to run against a live database while the old code is still serving —
the ``district`` column and its index are untouched, and a process running the
previous release keeps working through the deploy. 0003 copies the data across
and 0004 drops the column once nothing reads it.

``state`` loses its "Kerala" default here. Existing rows keep the value they
were written with; the default only ever applied to new inserts, and Goatza is
no longer a one-state list.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('shared', '0001_initial'),
        ('waitlist', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='playersignup',
            name='location',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='waitlist_signups',
                to='shared.location',
            ),
        ),
        migrations.AddField(
            model_name='playersignup',
            name='location_name',
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name='playersignup',
            name='city',
            field=models.CharField(blank=True, max_length=100),
        ),
        migrations.AlterField(
            model_name='playersignup',
            name='state',
            field=models.CharField(blank=True, max_length=100),
        ),
        migrations.AddField(
            model_name='playersignup',
            name='country_code',
            field=models.CharField(blank=True, max_length=5),
        ),
        migrations.AddField(
            model_name='playersignup',
            name='latitude',
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='playersignup',
            name='longitude',
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddIndex(
            model_name='playersignup',
            index=models.Index(fields=['city'], name='waitlist_pl_city_b81d2e_idx'),
        ),
        migrations.AddIndex(
            model_name='playersignup',
            index=models.Index(
                fields=['country_code'],
                name='waitlist_pl_country_57c88e_idx',
            ),
        ),
    ]
