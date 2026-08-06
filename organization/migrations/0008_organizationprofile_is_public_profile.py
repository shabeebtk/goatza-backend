from django.db import migrations, models


class Migration(migrations.Migration):
    """Public org profiles, on by default — see the accounts twin migration."""

    dependencies = [
        ("organization", "0007_organizationprofile_organizatio_followe_3e8bda_idx"),
    ]

    operations = [
        migrations.AddField(
            model_name="organizationprofile",
            name="is_public_profile",
            field=models.BooleanField(
                default=True,
                help_text=(
                    "Visible to logged-out visitors. "
                    "Does not affect in-app visibility."
                ),
            ),
        ),
    ]
